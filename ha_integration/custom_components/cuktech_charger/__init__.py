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
    DEVICE_INFO,
    TOPIC_PORT,
    TOPIC_PREFIX,
    TOPIC_PROBE,
    TOPIC_SETTINGS,
    TOPIC_STATUS,
    TOPIC_SET,
    PORT_MAP,
    PROTOCOL_BITS,
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
        self._health_check_unsub = None
        self._last_status_time: float = 0
        self._health_check_task = None
        self._health_failures = 0
        self._device_model: str = DEVICE_INFO["model"]
        self._firmware_version: str = ""
        self._ble_connected: bool = False
        self._ble_enabled: bool = False
        self._ble_pending: bool = False
        self._ble_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Return True if BLE server is reachable (MQTT connected or HTTP OK)."""
        return self._available

    @property
    def ble_connected(self) -> bool:
        """Return True if BLE device is actually connected."""
        return self._ble_connected

    @property
    def ble_enabled(self) -> bool:
        """Return True if BLE connection is enabled (user intent)."""
        return self._ble_enabled

    @property
    def ble_pending(self) -> bool:
        """Return True if a BLE connect/disconnect operation is in progress."""
        return self._ble_pending

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
        return dict(self._port_data)

    @property
    def data(self) -> dict[str, Any]:
        """Return settings data (copy)."""
        return dict(self._settings)

    @property
    def protocol_switches(self) -> dict[str, dict[str, bool]]:
        """Return decoded protocol switches from PIID 21."""
        v = self._settings.get("21", 0)
        result = {}
        for port, protos in PROTOCOL_BITS.items():
            result[port] = {}
            for proto, bit in protos.items():
                result[port][proto] = bool(v & (1 << bit))
        return result

    @staticmethod
    def _encode_protocol_extend(switches: dict) -> int:
        """Encode protocol switch dict to PIID 21 value."""
        def _c1c2_flags(ps):
            if not ps:
                return 0
            v = 0x08  # 保留位固定为 1
            if ps.get("pd"):   v |= 0x01
            if ps.get("pps"):  v |= 0x02
            if ps.get("ufcs"): v |= 0x04
            return v

        c1 = _c1c2_flags(switches.get("c1"))
        c2 = _c1c2_flags(switches.get("c2"))

        def _c3a_flags(ps):
            if not ps:
                return 0
            v = 0
            if ps.get("ufcs"): v |= 0x01
            if ps.get("scp"):  v |= 0x02
            return v

        c3 = _c3a_flags(switches.get("c3"))
        a = _c3a_flags(switches.get("a"))
        return (a << 24) | (c3 << 16) | (c2 << 8) | c1

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info with dynamic firmware version."""
        return {
            **DEVICE_INFO,
            "model": self._device_model or DEVICE_INFO["model"],
            "sw_version": self._firmware_version,
        }

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
        self._health_check_unsub = async_track_time_interval(
            self.hass, self._async_health_check, HEALTH_CHECK_INTERVAL
        )
        await self._async_health_check(None)

        # 首次加载时同步 BLE 开关状态与实际连接状态
        if self._ble_connected and not self._ble_enabled:
            self._ble_enabled = True
            _LOGGER.info("Initial BLE state synced: connected")

        _LOGGER.info("CUKTECH Charger MQTT coordinator set up successfully")

    async def async_unload(self) -> None:
        """Unload MQTT subscriptions."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._health_check_unsub:
            self._health_check_unsub()
        self._health_check_unsub = None
        _LOGGER.info("CUKTECH Charger MQTT coordinator unloaded")

    def _notify_callbacks(self) -> None:
        """Notify all registered callbacks."""
        for cb in list(self._callbacks):
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
            connected = payload.get("connected", False)
            prev_ble_connected = self._ble_connected
            ble_changed = prev_ble_connected != connected
            self._mqtt_connected = connected
            self._ble_connected = connected
            if self._mqtt_connected:
                self._last_status_time = time.time()
                self._health_failures = 0
            # Update device info from BLE server
            info_changed = False
            if "device_model" in payload and payload["device_model"]:
                if self._device_model != payload["device_model"]:
                    self._device_model = payload["device_model"]
                    info_changed = True
            if "firmware_version" in payload:
                new_fw = payload.get("firmware_version", "")
                if self._firmware_version != new_fw:
                    self._firmware_version = new_fw
                    info_changed = True
            self._update_availability()
            # 同步用户意图与 BLE 实际状态（仅跟随 MQTT 状态变化，不强制覆盖）
            ble_enabled_changed = False
            if not prev_ble_connected and self._ble_connected and not self._ble_enabled:
                self._ble_enabled = True
                ble_enabled_changed = True
                _LOGGER.info("BLE auto-reconnected, syncing switch state")
            elif prev_ble_connected and not self._ble_connected and self._ble_enabled:
                self._ble_enabled = False
                ble_enabled_changed = True
                _LOGGER.info("BLE disconnected, syncing switch state")
            # 如果 BLE 实际状态与用户意图一致，清除 pending
            if self._ble_pending and self._ble_connected == self._ble_enabled:
                self._ble_pending = False
                _LOGGER.debug("BLE state confirmed, cleared pending")
            if self._available and not was_available:
                _LOGGER.info("BLE server is now available (MQTT)")
            elif not self._available and was_available:
                _LOGGER.warning("BLE server disconnected (MQTT)")
            if info_changed or ble_changed or ble_enabled_changed:
                self.hass.async_create_task(self._async_update_device_registry())
                self._notify_callbacks()
            _LOGGER.debug("Status message: %s", payload)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Status JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Status message error: %s", err)

    async def _async_update_device_registry(self) -> None:
        """Update device registry with latest device info (firmware, model)."""
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, self.entry.entry_id)})
        if device is not None:
            dev_reg.async_update_device(
                device.id,
                sw_version=self._firmware_version or None,
                model=self._device_model or None,
            )

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
                    # Fallback: also read connection status and device info from HTTP if MQTT not connected
                    if not self._mqtt_connected:
                        try:
                            data = await resp.json()
                            info_changed = False
                            model = data.get("device_model", "")
                            fw = data.get("firmware_version", "")
                            ble_conn = data.get("connected", False)
                            if model and self._device_model != model:
                                self._device_model = model
                                info_changed = True
                            if fw and self._firmware_version != fw:
                                self._firmware_version = fw
                                info_changed = True
                            if self._ble_connected != ble_conn:
                                self._ble_connected = ble_conn
                                # 首次加载或 MQTT 断连时，同步开关状态到实际连接状态
                                if ble_conn and not self._ble_enabled:
                                    self._ble_enabled = True
                                elif not ble_conn and self._ble_enabled:
                                    self._ble_enabled = False
                                self._notify_callbacks()
                            if info_changed:
                                self.hass.async_create_task(self._async_update_device_registry())
                                self._notify_callbacks()
                        except Exception:
                            pass
                else:
                    self._health_failures += 1
                    if self._available:
                        _LOGGER.warning("BLE server returned HTTP status %d (failure #%d)", resp.status, self._health_failures)
                    elif self._health_failures % 10 == 0:
                        _LOGGER.warning("BLE server HTTP check failed %d times", self._health_failures)
                    self._available = self._mqtt_connected
        except Exception as err:
            self._health_failures += 1
            if self._available:
                _LOGGER.warning("BLE server HTTP health check failed: %s", err)
            elif self._health_failures % 10 == 0:
                _LOGGER.warning("BLE server HTTP check failed %d times: %s", self._health_failures, err)
            self._available = self._mqtt_connected

    async def async_enable_ble(self, enable: bool) -> bool:
        """Enable or disable BLE connection via MQTT + HTTP API."""
        async with self._ble_lock:
            self._ble_enabled = enable
            self._ble_pending = True
            self._notify_callbacks()

            # 30 秒超时保护：防止网络异常时 switch 永远灰掉
            async def _clear_pending_after_delay() -> None:
                await asyncio.sleep(30)
                if self._ble_pending:
                    self._ble_pending = False
                    self._notify_callbacks()
                    _LOGGER.warning("BLE operation timed out, clearing pending state")

            timeout_task = self.hass.async_create_task(_clear_pending_after_delay())

            try:
                # MQTT (ESP32)
                await mqtt.async_publish(
                    self.hass, f"{TOPIC_PREFIX}/ble",
                    json.dumps({"enabled": enable})
                )
                _LOGGER.info("BLE %s published via MQTT", "enable" if enable else "disable")
            except Exception as err:
                _LOGGER.debug("MQTT BLE publish failed: %s", err)

            try:
                # HTTP (ble_server)
                session = async_get_clientsession(self.hass)
                url = f"{self.server_url}/api/enable"
                async with session.post(url, json={"enabled": enable}, timeout=30) as resp:
                    if resp.status == 200:
                        _LOGGER.info("BLE connection %s via HTTP", "enabled" if enable else "disabled")
            except Exception as err:
                _LOGGER.debug("HTTP BLE control not available: %s", err)
            finally:
                self._ble_pending = False
                self._notify_callbacks()
                if not timeout_task.done():
                    timeout_task.cancel()
            return True

    async def async_set_value(self, piid: int, value: Any) -> None:
        """Set a PIID value via MQTT."""
        try:
            await mqtt.async_publish(
                self.hass, TOPIC_SET, json.dumps({"piid": piid, "value": value})
            )
        except Exception as err:
            _LOGGER.error("Failed to publish MQTT command: %s", err)

    async def async_port_control(self, port: str, action: str) -> None:
        """Control a port (on/off) via MQTT."""
        try:
            await mqtt.async_publish(
                self.hass, TOPIC_PORT, json.dumps({"port": port, "action": action})
            )
        except Exception as err:
            _LOGGER.error("Failed to publish MQTT command: %s", err)

    async def async_set_protocol(self, port: str, protocol: str, on: bool) -> None:
        """Set a protocol switch on/off via MQTT."""
        async with self._ble_lock:
            switches = self.protocol_switches
            if port not in switches or protocol not in switches[port]:
                _LOGGER.error("Unknown protocol switch: %s.%s", port, protocol)
                return
            switches[port][protocol] = on
            value = self._encode_protocol_extend(switches)
            await self.async_set_value(21, value)

