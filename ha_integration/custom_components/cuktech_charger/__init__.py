"""CUKTECH Charger integration for Home Assistant - MQTT based."""
from __future__ import annotations

import asyncio
import json
import logging
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
    TOPIC_SETTINGS,
    TOPIC_STATUS,
    TOPIC_SET,
    TOPIC_CHARGE_EVENT,
    PORT_MAP,
    PROTOCOL_BITS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.EVENT]
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
        self._port_callbacks: list = []
        self._settings_callbacks: list = []
        self._unsub: list = []
        self._available = False
        self._mqtt_connected = False
        self._health_check_unsub = None
        self._last_status_time: float = -999
        self._health_check_task = None
        self._health_failures = 0
        self._device_model: str = DEVICE_INFO["model"]
        self._firmware_version: str = ""
        self._ble_connected: bool = False
        self._ble_enabled: bool = False
        self._ble_pending: bool = False
        self._ble_lock = asyncio.Lock()
        self._ble_timeout_task: asyncio.Task | None = None
        self._charge_events: list[dict] = []
        self._charge_event_callbacks: list = []

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

    # --- Callback registration ---

    def register_callback(self, cb) -> None:
        """Register a callback for all state updates."""
        self._callbacks.append(cb)

    def unregister_callback(self, cb) -> None:
        """Unregister a generic callback."""
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    def register_port_callback(self, cb) -> None:
        """Register a callback for port data updates only."""
        self._port_callbacks.append(cb)

    def unregister_port_callback(self, cb) -> None:
        """Unregister a port callback."""
        if cb in self._port_callbacks:
            self._port_callbacks.remove(cb)

    def register_settings_callback(self, cb) -> None:
        """Register a callback for settings data updates only."""
        self._settings_callbacks.append(cb)

    def unregister_settings_callback(self, cb) -> None:
        """Unregister a settings callback."""
        if cb in self._settings_callbacks:
            self._settings_callbacks.remove(cb)

    def register_charge_event_callback(self, cb) -> None:
        """Register a callback for charge completion events."""
        self._charge_event_callbacks.append(cb)

    def unregister_charge_event_callback(self, cb) -> None:
        """Unregister a charge event callback."""
        if cb in self._charge_event_callbacks:
            self._charge_event_callbacks.remove(cb)

    # --- Internal notification methods ---

    def _notify_callbacks(self, cbs: list | None = None) -> None:
        """Notify all registered callbacks in a given list, or all if none given."""
        targets = cbs if cbs is not None else self._callbacks
        for cb in list(targets):
            try:
                cb()
            except Exception:
                _LOGGER.exception("Callback error")

    def _notify_all(self) -> None:
        """Notify all callback lists (generic + port + settings)."""
        self._notify_callbacks(self._callbacks)
        self._notify_callbacks(self._port_callbacks)
        self._notify_callbacks(self._settings_callbacks)

    # --- Properties ---

    @property
    def last_charge_event(self) -> dict | None:
        """Return the most recent charge event, or None."""
        return self._charge_events[-1] if self._charge_events else None

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

    # --- Lifecycle ---

    async def async_setup(self) -> None:
        """Set up MQTT subscriptions."""
        await self._async_wait_mqtt_ready()

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

        unsub = await mqtt.async_subscribe(
            self.hass, TOPIC_CHARGE_EVENT, self._on_charge_event
        )
        self._unsub.append(unsub)

        self._last_status_time = self.hass.loop.time()

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
        # Cancel any in-flight BLE timeout task
        if self._ble_timeout_task is not None and not self._ble_timeout_task.done():
            self._ble_timeout_task.cancel()
            self._ble_timeout_task = None

        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._health_check_unsub:
            self._health_check_unsub()
        self._health_check_unsub = None
        _LOGGER.info("CUKTECH Charger MQTT coordinator unloaded")

    async def _async_wait_mqtt_ready(self) -> None:
        """Wait for the MQTT client (setup) to become available.

        使用官方 mqtt.async_wait_for_mqtt_client()：当 MQTT 集成已加载/客户端可用时
        立即返回，否则内部等至 AVAILABILITY_TIMEOUT（50s），超时或未启用 MQTT 时返回
        False，由 HA 稍后重试整个 setup，避免启动时长时间阻塞。
        """
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise ConfigEntryNotReady("MQTT not available")

    # --- Device info synchronization (shared logic) ---

    def _sync_device_info_from_payload(self, payload: dict) -> bool:
        """Update device model/firmware from a payload dict. Returns True if any changed."""
        changed = False
        if "device_model" in payload and payload["device_model"]:
            if self._device_model != payload["device_model"]:
                self._device_model = payload["device_model"]
                changed = True
        if "firmware_version" in payload:
            new_fw = payload.get("firmware_version", "")
            if self._firmware_version != new_fw:
                self._firmware_version = new_fw
                changed = True
        return changed

    def _sync_ble_state(self, connected: bool) -> bool:
        """Sync BLE state from actual connection. Returns True if enabled state changed."""
        prev_enabled = self._ble_enabled
        prev_connected = self._ble_connected
        self._ble_connected = connected
        if connected and not self._ble_enabled:
            self._ble_enabled = True
            _LOGGER.info("BLE auto-reconnected, syncing switch state")
        elif not connected and self._ble_enabled:
            self._ble_enabled = False
            _LOGGER.info("BLE disconnected, syncing switch state")
        if prev_connected and not connected:
            # 设备断开：清空端口数据，避免实体展示陈旧读数（设置等保留，重连后由服务器重新发布）
            self._port_data = {}
            self._notify_callbacks(self._port_callbacks)
            _LOGGER.info("BLE disconnected, cleared stale port data")
        return self._ble_enabled != prev_enabled

    def _clear_pending_if_confirmed(self) -> None:
        """Clear BLE pending if actual state matches user intent."""
        if self._ble_pending and self._ble_connected == self._ble_enabled:
            self._ble_pending = False
            _LOGGER.debug("BLE state confirmed, cleared pending")

    def _log_health_failure(self, message: str, err: str = "") -> None:
        """Log health check failure with throttling."""
        self._health_failures += 1
        if self._available:
            _LOGGER.warning("%s%s", message, f": {err}" if err else "")
        elif self._health_failures % 10 == 0:
            _LOGGER.warning(
                "%s (failure #%d)%s", message, self._health_failures, f": {err}" if err else ""
            )

    # --- MQTT message handlers ---

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
                self._notify_callbacks(self._port_callbacks)
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
            self._notify_callbacks(self._settings_callbacks)
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
            prev_ble_connected = self._ble_connected
            connected = payload.get("connected", False)

            self._mqtt_connected = connected
            if connected:
                self._last_status_time = self.hass.loop.time()
                self._health_failures = 0

            # Sync device info & BLE state from payload
            info_changed = self._sync_device_info_from_payload(payload)
            self._sync_ble_state(connected)
            self._clear_pending_if_confirmed()
            self._update_availability()

            # Log availability transitions
            if self._available and not was_available:
                _LOGGER.info("BLE server is now available (MQTT)")
            elif not self._available and was_available:
                _LOGGER.warning("BLE server disconnected (MQTT)")

            if info_changed or prev_ble_connected != connected:
                self.hass.async_create_task(self._async_update_device_registry())

            self._notify_callbacks(self._callbacks)
            _LOGGER.debug("Status message: %s", payload)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Status JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Status message error: %s", err)

    @callback
    def _on_charge_event(self, msg: Any) -> None:
        """Handle charge completion event from MQTT."""
        try:
            payload = json.loads(msg.payload)
            if payload.get("event") != "charge_end":
                return
            self._charge_events.append(payload)
            # Keep only last 50 events
            if len(self._charge_events) > 50:
                self._charge_events = self._charge_events[-50:]
            _LOGGER.info("Charge event: port=%s energy=%.1fWh duration=%ds",
                         payload.get("port"), payload.get("energy_wh", 0),
                         payload.get("duration_sec", 0))
            self._notify_callbacks(self._charge_event_callbacks)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Charge event JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Charge event error: %s", err)

    # --- Device registry ---

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

    # --- Availability ---

    def _update_availability(self) -> None:
        """Update availability based on MQTT status and HTTP health."""
        http_recent = (self.hass.loop.time() - self._last_status_time) < 30
        self._available = self._mqtt_connected or http_recent

    async def _async_health_check(self, _now) -> None:
        """Check if BLE server is reachable via HTTP."""
        session = async_get_clientsession(self.hass)
        try:
            url = f"{self.server_url}/api/status"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    was_available = self._available
                    self._last_status_time = self.hass.loop.time()
                    self._health_failures = 0
                    self._update_availability()
                    if self._available and not was_available:
                        _LOGGER.info("BLE server is now available (HTTP)")
                    # Fallback: also read connection status and device info from HTTP if MQTT not connected
                    if not self._mqtt_connected:
                        await self._async_health_check_parse_body(resp)
                else:
                    self._log_health_failure(
                        f"BLE server returned HTTP status {resp.status}"
                    )
                    self._available = self._mqtt_connected
        except Exception as err:
            self._log_health_failure("BLE server HTTP health check failed", str(err))
            self._available = self._mqtt_connected

    async def _async_health_check_parse_body(self, resp) -> None:
        """Parse health check JSON body and sync device info."""
        try:
            data = await resp.json()
            info_changed = self._sync_device_info_from_payload(data)
            ble_conn = data.get("connected", False)
            if self._ble_connected != ble_conn:
                self._sync_ble_state(ble_conn)
                self._clear_pending_if_confirmed()
                self._notify_callbacks(self._callbacks)
            if info_changed:
                self.hass.async_create_task(self._async_update_device_registry())
                self._notify_callbacks(self._callbacks)
        except Exception as err:
            _LOGGER.warning("Failed to parse health check JSON response: %s", err)

    # --- BLE control ---

    async def async_enable_ble(self, enable: bool) -> bool:
        """Enable or disable BLE connection via MQTT (primary) + HTTP (fallback)."""
        async with self._ble_lock:
            # Cancel any previous pending timeout
            if self._ble_timeout_task is not None and not self._ble_timeout_task.done():
                self._ble_timeout_task.cancel()
                self._ble_timeout_task = None

            self._ble_enabled = enable
            self._ble_pending = True
            self._notify_callbacks(self._callbacks)

            async def _clear_pending_after_delay() -> None:
                await asyncio.sleep(30)
                if self._ble_pending:
                    self._ble_pending = False
                    self._notify_callbacks(self._callbacks)
                    _LOGGER.warning("BLE operation timed out, clearing pending state")

            self._ble_timeout_task = self.hass.async_create_task(_clear_pending_after_delay())

            success = False
            # MQTT (primary channel - ESP32)
            try:
                await mqtt.async_publish(
                    self.hass, f"{TOPIC_PREFIX}/ble",
                    json.dumps({"enabled": enable})
                )
                _LOGGER.info("BLE %s published via MQTT", "enable" if enable else "disable")
                success = True
            except Exception as err:
                _LOGGER.debug("MQTT BLE publish failed: %s", err)

            # HTTP (fallback - ble_server)
            if not success:
                try:
                    session = async_get_clientsession(self.hass)
                    url = f"{self.server_url}/api/enable"
                    async with session.post(url, json={"enabled": enable}, timeout=30) as resp:
                        if resp.status == 200:
                            _LOGGER.info("BLE connection %s via HTTP (fallback)", "enabled" if enable else "disabled")
                            success = True
                except Exception as err:
                    _LOGGER.warning("HTTP BLE control also failed: %s", err)

            self._ble_pending = False
            self._notify_callbacks(self._callbacks)
            if self._ble_timeout_task is not None and not self._ble_timeout_task.done():
                self._ble_timeout_task.cancel()
            self._ble_timeout_task = None
            return success

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
