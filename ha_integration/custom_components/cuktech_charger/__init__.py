"""CUKTECH Charger integration for Home Assistant - MQTT based."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Any

import homeassistant.components.mqtt as mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    CONF_SERVER_URL,
    DEFAULT_SERVER_URL,
    TOPIC_PORT,
    TOPIC_PREFIX,
    TOPIC_PROBE,
    TOPIC_SETTINGS,
    TOPIC_STATUS,
    TOPIC_SET,
    PORT_MAP,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.BINARY_SENSOR, Platform.NUMBER]
HEALTH_CHECK_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CUKTECH Charger from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = CuktechMQTTCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator

    try:
        await coordinator.async_setup()
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        _LOGGER.exception("Failed to set up coordinator")
        raise ConfigEntryNotReady from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_unload()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


class CuktechMQTTCoordinator:
    """Coordinator for CUKTECH Charger MQTT communication."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.server_url = entry.data.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
        self._port_data: dict[str, dict[str, Any]] = {}
        self._settings: dict[str, Any] = {}
        self._callbacks: list = []
        self._unsub: list = []
        self._available = False
        self._mqtt_connected = False
        self._last_status_time: float = 0
        self._health_check_task = None
        self._health_failures = 0

    @property
    def available(self) -> bool:
        """Return True if BLE server is reachable (MQTT connected or HTTP OK)."""
        return self._available

    def register_callback(self, cb) -> None:
        """Register a callback for state updates."""
        if len(self._callbacks) > 100:
            _LOGGER.warning("Too many callbacks registered: %d", len(self._callbacks))
        self._callbacks.append(cb)

    def unregister_callback(self, cb) -> None:
        """Unregister a callback."""
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    @property
    def port_data(self) -> dict[str, dict[str, Any]]:
        """Return port data."""
        return self._port_data

    @property
    def data(self) -> dict[str, Any]:
        """Return settings data (copy)."""
        return dict(self._settings)

    async def async_setup(self) -> None:
        """Set up MQTT subscriptions."""
        base_delay = 1
        max_delay = 30
        for attempt in range(10):
            try:
                await mqtt.async_publish(self.hass, TOPIC_PROBE, "ready")
                break
            except Exception as err:
                delay = min(base_delay * (2 ** attempt), max_delay)
                if attempt < 9:
                    _LOGGER.debug("MQTT not ready, attempt %d/%d, retrying in %ds: %s", attempt + 1, 10, delay, err)
                    await asyncio.sleep(delay)
                else:
                    _LOGGER.error("MQTT not ready after 10 attempts")
                    raise ConfigEntryNotReady("MQTT not available")

        for port_name in ("c1", "c2", "c3", "a"):
            unsub = await mqtt.async_subscribe(
                self.hass, f"{TOPIC_PORT}/{port_name}", self._on_port_message
            )
            self._unsub.append(unsub)

        unsub = await mqtt.async_subscribe(
            self.hass, TOPIC_SETTINGS, self._on_settings_message
        )
        self._unsub.append(unsub)

        unsub = await mqtt.async_subscribe(
            self.hass, TOPIC_STATUS, self._on_status_message
        )
        self._unsub.append(unsub)

        self._last_status_time = time.time()

        # Start HTTP health check as fallback
        self._health_check_task = async_track_time_interval(
            self.hass, self._async_health_check, HEALTH_CHECK_INTERVAL
        )
        await self._async_health_check(None)

        _LOGGER.info("CUKTECH Charger MQTT coordinator set up successfully")

    async def async_unload(self) -> None:
        """Unload MQTT subscriptions."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._health_check_task:
            self._health_check_task()
            self._health_check_task = None
        _LOGGER.info("CUKTECH Charger MQTT coordinator unloaded")

    def _notify_callbacks(self) -> None:
        """Notify all registered callbacks."""
        for cb in self._callbacks:
            try:
                cb()
            except Exception as err:
                _LOGGER.exception("Callback error: %s", err)

    @callback
    def _on_port_message(self, msg: Any) -> None:
        """Handle port data message."""
        try:
            payload = json.loads(msg.payload)
            topic_parts = msg.topic.split("/")
            port_name = topic_parts[-1]
            piid = PORT_MAP.get(port_name)
            if piid:
                _LOGGER.debug("Port %s: voltage=%s current=%s power=%s protocol=%s",
                    port_name, payload.get("voltage"), payload.get("current"),
                    payload.get("power"), payload.get("protocol"))
                self._port_data[str(piid)] = payload
                self._notify_callbacks()
        except json.JSONDecodeError as err:
            _LOGGER.debug("Port JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Port message error: %s", err)

    @callback
    def _on_settings_message(self, msg: Any) -> None:
        """Handle settings message."""
        try:
            payload = json.loads(msg.payload)
            _LOGGER.debug("Settings updated: %s", list(payload.keys()))
            self._settings = payload
            self._notify_callbacks()
        except json.JSONDecodeError as err:
            _LOGGER.debug("Settings JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Settings message error: %s", err)

    @callback
    def _on_status_message(self, msg: Any) -> None:
        """Handle status message from MQTT."""
        try:
            payload = json.loads(msg.payload)
            was_available = self._available
            self._mqtt_connected = payload.get("connected", False)
            self._last_status_time = time.time()
            if self._mqtt_connected:
                self._health_failures = 0
            self._update_availability()
            if self._available and not was_available:
                _LOGGER.info("BLE server is now available (MQTT)")
            elif not self._available and was_available:
                _LOGGER.warning("BLE server disconnected (MQTT)")
            _LOGGER.debug("Status message: %s", payload)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Status JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Status message error: %s", err)

    def _update_availability(self) -> None:
        """Update availability based on MQTT status and HTTP health."""
        http_recent = (time.time() - self._last_status_time) < 30
        self._available = self._mqtt_connected or http_recent

    async def _async_health_check(self, _now) -> None:
        """Check if BLE server is reachable via HTTP."""
        session = async_get_clientsession(self.hass)
        try:
            url = f"{self.server_url}/api/status"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    was_available = self._available
                    self._last_status_time = time.time()
                    self._health_failures = 0
                    self._update_availability()
                    if self._available and not was_available:
                        _LOGGER.info("BLE server is now available (HTTP)")
                else:
                    self._health_failures += 1
                    if self._available:
                        _LOGGER.warning("BLE server returned HTTP status %d (failure #%d)", resp.status, self._health_failures)
                    elif self._health_failures % 10 == 0:
                        _LOGGER.warning("BLE server HTTP check failed %d times", self._health_failures)
                    self._available = False
        except Exception as err:
            self._health_failures += 1
            if self._available:
                _LOGGER.warning("BLE server HTTP health check failed: %s", err)
            elif self._health_failures % 10 == 0:
                _LOGGER.warning("BLE server HTTP check failed %d times: %s", self._health_failures, err)
            self._available = False

    async def async_set_value(self, piid: int, value: Any) -> None:
        """Set a PIID value via MQTT."""
        await mqtt.async_publish(
            self.hass, TOPIC_SET, json.dumps({"piid": piid, "value": value})
        )

    async def async_port_control(self, port: str, action: str) -> None:
        """Control a port (on/off) via MQTT."""
        await mqtt.async_publish(
            self.hass, TOPIC_PORT, json.dumps({"port": port, "action": action})
        )
