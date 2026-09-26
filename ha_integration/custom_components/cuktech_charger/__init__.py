"""CUKTECH Charger integration for Home Assistant - MQTT based."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

import homeassistant.components.mqtt as mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_SERVER_URL,
    CONF_HOST,
    DEFAULT_SERVER_URL,
    DEVICE_INFO,
    TOPIC_PORT,
    TOPIC_PREFIX,
    TOPIC_SETTINGS,
    TOPIC_STATUS,
    TOPIC_SET,
    TOPIC_CHARGE_EVENT,
    PORT_MAP,
    HEALTH_CHECK_INTERVAL,
    HTTP_TIMEOUT,
    BLE_OPERATION_TIMEOUT,
    CHARGE_EVENT_BUFFER,
    STATUS_STALE_SECONDS,
    LIMIT_STORE_VERSION,
    LIMIT_BACKEND_LOCAL,
    LIMIT_BACKEND_SERVER,
    LIMIT_POLL_INTERVAL,
    LIMIT_MODES,
    PIID_TO_PORT,
)
from .energy_engine import (
    ChargeLimitTracker,
    END_REASON_LINK_LOSS,
    END_REASON_SHUTDOWN,
)
from .protocol_codec import (
    decode_protocol_switches,
    encode_protocol_switches,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.EVENT]


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
        # 地址解析顺序：新键 -> 旧键(host) -> 默认值。旧条目只有 "host"，
        # 而 entry.data.get(CONF_SERVER_URL) 对缺失键返回 None 时若直接给默认值
        # 就会丢掉用户真实配置的地址（服务端不在 localhost 时表现最明显）。
        self.server_url = (
            entry.data.get(CONF_SERVER_URL)
            or entry.data.get(CONF_HOST)
            or DEFAULT_SERVER_URL
        )
        self._port_data: dict[str, dict[str, Any]] = {}
        self._settings: dict[str, Any] = {}
        self._callbacks: list = []
        self._port_callbacks: list = []
        self._settings_callbacks: list = []
        self._unsub: list = []
        self._available = False
        # 注意: 字段名为历史遗留，实际语义是「最近一条 status 消息里的
        # BLE 设备 connected 标记」，而非 MQTT 客户端是否连接。
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
        # 有界缓冲: 自动丢弃最旧的事件，避免长时间运行内存增长
        self._charge_events: deque[dict] = deque(maxlen=CHARGE_EVENT_BUFFER)
        self._charge_event_callbacks: list = []
        # 已处理事件的去重键: set 提供 O(1) 查找，deque 维护插入顺序以便裁剪最旧的键。
        # 依赖 set 迭代顺序裁剪是不可靠的 (set 无序)，故用 deque 显式记录顺序。
        self._charge_event_keys: set = set()
        self._charge_event_key_order: deque = deque()

        # ── 充电量限额 ──
        # 积分/判定/会话边界全部在 energy_engine（纯逻辑，可脱离 HA 单测）；
        # 这里只负责：喂样本、按 due_cutoffs() 发布关断命令、持久化。
        self._charge_limits = ChargeLimitTracker()
        self._limits_store = Store(
            hass, LIMIT_STORE_VERSION, f"{DOMAIN}.charge_limits.{entry.entry_id}"
        )
        # local: HA 自算（两端通用，含 ESP32）
        # server: 委派给 Python BLE server /api/charge-limits（见 _async_probe_limit_backend）
        self._limit_backend = LIMIT_BACKEND_LOCAL
        self._limit_poll_unsub = None
        self._limits_loaded = False
        # 委派模式下服务端的权威快照 {port: {wh,mode,fired,session_wh,is_charging}}。
        # 刻意不写进 _charge_limits：本地引擎若也持有同一份 wh，本地 end_session
        # 会把 once 限额"消费"掉，导致实体显示被静默清零（与服务端不一致）。
        self._server_limits: dict[str, dict] = {}
        # 最近一次落盘的限额快照：用于发现"非用户操作导致的限额变化"
        # （once 限额被消费发生在引擎内部，见 _persist_limits_if_changed）
        self._saved_limits: dict | None = None

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
        return decode_protocol_switches(self._settings.get("21", 0))

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

        # 端口名统一取自 PORT_MAP (const.py 单一数据源)，避免此处硬编码漂移
        for port_name in PORT_MAP:
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

        # 限额配置：先恢复 Store，再探测后端。顺序有意义——万一 REST 可用，
        # 服务端返回的权威值会覆盖本地快照，避免重启后短暂显示陈旧值。
        await self._async_load_limits()
        await self._async_probe_limit_backend()

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
        # HA 停止不是用户意图终止充电：结束会话但保留 once 限额
        # （否则一次 HA 重启就静默解除用户刚设的限制，见 END_REASONS_PRESERVING_LIMIT）
        self._charge_limits.end_session_all(
            END_REASON_SHUTDOWN, self.hass.loop.time())
        if self._limit_poll_unsub:
            self._limit_poll_unsub()
            self._limit_poll_unsub = None
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
            # 以 link_loss 结束会话（对齐 ble_manager.py:900 断连时的处理）。
            # 链路中断属基础设施故障、会话可续，因此"保留" once 限额——否则重连
            # 之后端口报 inactive，会被判成 USER_OFF 而把用户刚设的一次性限额吃掉。
            self._charge_limits.end_session_all(
                END_REASON_LINK_LOSS, self.hass.loop.time())
            self._persist_limits_if_changed()
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
            if not isinstance(payload, dict):
                _LOGGER.debug("Port message payload is not an object, ignored: %r", payload)
                return
            topic_parts = msg.topic.split("/")
            port_name = topic_parts[-1]
            piid = PORT_MAP.get(port_name)
            if piid:
                _LOGGER.debug("Port %s: voltage=%s current=%s power=%s protocol=%s",
                    port_name, payload.get("voltage"), payload.get("current"),
                    payload.get("power"), payload.get("protocol"))
                self._port_data[str(piid)] = payload
                # 喂给本地限额引擎：V×I 梯形积分 + 会话边界判定。
                # 无论后端是 Python server 还是 ESP32，这个 topic 承载的都是同一份
                # 1Hz 设备推送，所以两种后端都能用同一套本地计量。
                self._async_ingest_port(piid, payload)
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
            if not isinstance(payload, dict):
                _LOGGER.debug("Settings message payload is not an object, ignored: %r", payload)
                return
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
            if not isinstance(payload, dict):
                _LOGGER.debug("Status message payload is not an object, ignored: %r", payload)
                return
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
            if not isinstance(payload, dict):
                _LOGGER.debug("Charge event payload is not an object, ignored: %r", payload)
                return
            if payload.get("event") != "charge_end":
                return
            # 去重: MQTT 重投递 / 服务器重启后重发同一事件时不应重复触发。
            # 不能只用 session_id: ble_server 在记录关闭/会话未落库时
            # session_id 恒为 0，同一端口两次真实不同的未落库事件会碰撞，
            # 使后一次被误判为重复而丢弃。故以 (port, end_time) 为键：
            # 两者在每条事件里都始终存在且唯一 (同一端口两次结束时间不会
            # 精确到同一秒)，跨端口又有 port 区分。
            dedup_key = (payload.get("port"), payload.get("end_time"))
            if dedup_key in self._charge_event_keys:
                _LOGGER.debug("Ignoring duplicate charge event (key=%s)", dedup_key)
                return
            self._charge_event_keys.add(dedup_key)
            # 用 deque 记录插入顺序；超出上限时丢弃最旧的键，与事件缓冲同步。
            # 顺序由插入决定，不依赖 set 的迭代顺序。
            self._charge_event_key_order.append(dedup_key)
            while len(self._charge_event_keys) > CHARGE_EVENT_BUFFER:
                oldest = self._charge_event_key_order.popleft()
                self._charge_event_keys.discard(oldest)

            self._charge_events.append(payload)
            _LOGGER.info("Charge event: port=%s energy=%.1fWh duration=%ds",
                         payload.get("port"), payload.get("energy_wh", 0),
                         payload.get("duration_sec", 0))
            self._notify_callbacks(self._charge_event_callbacks)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Charge event JSON parse error: %s", err)
        except Exception as err:
            _LOGGER.exception("Charge event error: %s", err)

    # --- Charge limits (充电量限额) ---

    @property
    def limit_backend(self) -> str:
        """Return the active charge-limit backend ("local" / "server")."""
        return self._limit_backend

    def _async_ingest_port(self, piid: int, payload: dict) -> None:
        """Feed one port sample into the local energy engine and act on cutoff.

        Always meters, under BOTH backends: the ESP32 has no energy accounting
        at all, and in `server` mode metering locally is still what makes the
        session-energy readout live and what keeps us functional if the REST
        API later goes away. Only *enforcement* is backend-specific (the server
        owns accumulation and cutoff when delegated), never accumulation.
        """
        try:
            voltage = float(payload.get("voltage") or 0.0)
            current = float(payload.get("current") or 0.0)
        except (TypeError, ValueError):
            return
        active = bool(payload.get("active", False))
        port = PIID_TO_PORT.get(piid)
        if port is None:
            return
        self._charge_limits.ingest(port, voltage, current, active, self.hass.loop.time())
        self._persist_limits_if_changed()
        self._async_enforce_limits()

    def _persist_limits_if_changed(self) -> None:
        """Persist limits that changed on their own, without a user action.

        Limits normally change through `async_set_charge_limit`, which saves.
        But a `once` limit is consumed *inside* the engine (end_session), and
        without this hook the consumed value never reaches disk — the next HA
        restart would reload it and silently re-arm a one-shot that had already
        fired. ble_server persists consumption (_persist_limits_async), so this
        keeps the two backends consistent.

        Cheap: comparing a 4-entry dict once per sample, and it only writes on
        an actual change (i.e. once per consumption).
        """
        snapshot = self._charge_limits.snapshot_limits()
        if snapshot == self._saved_limits:
            return
        self._saved_limits = snapshot
        self.hass.async_create_task(self._async_save_limits())
        # number/select 订阅的是 settings 回调，而消费发生在 port 回调路径上。
        # 不在这里补一次通知的话，local 模式下实体要等到下一条 settings 消息
        # 才显示"限额已被消费"（ESP32 的 settings 轮询是 20~90s 量级）。
        self._notify_callbacks(self._settings_callbacks)

    def _async_enforce_limits(self) -> None:
        """Publish port-off for any port whose limit has been reached.

        Sole enforcement gate: skipped when the Python server owns limits, so
        the two implementations can't race each other into double cutoffs.
        """
        if self._limit_backend != LIMIT_BACKEND_LOCAL:
            return
        self._enforce_now()

    def _enforce_now(self) -> None:
        """Evaluate due cutoffs and publish port-off for each.

        The off topic is the integration's normal port-control channel and is
        implemented by BOTH backends:
            Python: ha_server.py subscribes {prefix}/port -> MIOT SET PIID 16
            ESP32:  main/main.c:356 subscribes {prefix}/port -> CMD_SET PIID 16
        so this single path cuts power under either firmware.
        """
        due = self._charge_limits.due_cutoffs(self.hass.loop.time())
        if not due:
            return
        for port in due:
            _LOGGER.info("Charge limit reached on port %s, switching off", port)
            self.hass.async_create_task(self.async_port_control(port, "off"))

    async def async_set_charge_limit(self, port: str, wh: float, mode=None) -> None:
        """Set one port's charge limit, routed to the active backend."""
        if port not in PORT_MAP:
            _LOGGER.error("Unknown port for charge limit: %s", port)
            return
        if self._limit_backend == LIMIT_BACKEND_SERVER:
            ok = await self._async_post_limit(port, wh, mode)
            if ok:
                return  # authoritative value arrives via next poll
            # REST failed mid-session: take over locally rather than silently
            # dropping the user's intent (the limit then still cuts power).
            _LOGGER.warning(
                "Server rejected charge limit for %s; taking over locally", port)
            self._adopt_server_limits_locally()
        self._charge_limits.set_limit(port, wh, mode)
        await self._async_save_limits()
        # Keep the change-detector in sync so the next sample doesn't re-save.
        self._saved_limits = self._charge_limits.snapshot_limits()
        self._notify_callbacks(self._settings_callbacks)

    async def _async_load_limits(self) -> None:
        """Restore persisted limits and accumulated totals from Store."""
        try:
            data = await self._limits_store.async_load()
        except Exception as err:
            _LOGGER.warning("Failed to load charge limits from store: %s", err)
            data = None
        if isinstance(data, dict):
            self._charge_limits.load_limits(data.get("limits"))
            # total_wh 持久化：避免因 HA 重启丢失累计电量
            self._charge_limits.restore_totals(data.get("totals"))
        # Baseline for _persist_limits_if_changed: whatever we just loaded is
        # by definition already on disk.
        self._saved_limits = self._charge_limits.snapshot_limits()
        self._limits_loaded = True

    async def _async_save_limits(self) -> None:
        """Persist limits + accumulated totals (debounced by the caller)."""
        payload = {
            "limits": self._charge_limits.snapshot_limits(),
            "totals": self._charge_limits.snapshot_totals(),
        }
        try:
            await self._limits_store.async_save(payload)
        except Exception as err:
            _LOGGER.warning("Failed to save charge limits: %s", err)

    async def _async_limit_request(self, body: dict | None = None) -> tuple[dict | None, str]:
        """GET (or POST when `body` is given) /api/charge-limits.

        Returns `(limits, reason)`: `limits` is the server's `limits` mapping, or
        None on every failure mode — transport error, non-200, unparsable body,
        missing/non-dict `limits`. `reason` is a short human-readable why, which
        the startup probe surfaces (it is the difference between "ESP32, no such
        API" and "server is down", and that is otherwise invisible).

        The startup probe, the keep-sync poll and the write path differ only in
        what they do with the answer, so they share this one implementation.
        """
        url = f"{self.server_url}/api/charge-limits"
        session = async_get_clientsession(self.hass)
        try:
            if body is None:
                request = session.get(url, timeout=HTTP_TIMEOUT)
            else:
                request = session.post(url, json=body, timeout=HTTP_TIMEOUT)
            async with request as resp:
                if resp.status != 200:
                    await resp.read()
                    return None, f"HTTP {resp.status}"
                try:
                    data = await resp.json()
                except Exception as err:
                    await resp.read()
                    return None, f"unparsable response ({err})"
        except Exception as err:
            return None, str(err)

        limits = (data or {}).get("limits")
        if not isinstance(limits, dict):
            return None, "response has no 'limits' object"
        return limits, "ok"

    async def _async_probe_limit_backend(self) -> None:
        """Detect whether the HTTP backend can own charge limits (route B).

        A Python ble_server answers GET /api/charge-limits with 200 -> delegate
        configuration there so HA and the Web UI stay in sync.

        An ESP32 has no such endpoint (see esp32_ble/main/http_server.c URI
        table) and returns 404, so we stay `local` — which is also the only
        viable mode there, since the firmware does no energy accounting at all.

        Probe failures are non-fatal: local mode is fully functional, so we
        never block setup on this.
        """
        limits, reason = await self._async_limit_request()
        if limits is None:
            self._log_local_mode(reason)
            return
        self._limit_backend = LIMIT_BACKEND_SERVER
        self._apply_server_limits(limits)
        # 服务端 Web UI 也可能改配置/推进进度；轮询既同步配置也同步 is_charging。
        self._limit_poll_unsub = async_track_time_interval(
            self.hass, self._async_poll_server_limits, LIMIT_POLL_INTERVAL
        )
        _LOGGER.info(
            "Charge limits delegated to BLE server (%s); entities mirror it",
            self.server_url,
        )

    def _log_local_mode(self, reason: str) -> None:
        """Report why limits are metered locally instead of delegated.

        INFO, not DEBUG: choosing `local` is a normal, fully functional outcome
        (ESP32 always lands here), but it also explains why HA would *not* be
        mirroring the BLE server's Web UI — which is otherwise very hard to
        diagnose from the entities alone.
        """
        _LOGGER.info(
            "Charge limits metered locally (no BLE server API at %s: %s); "
            "HA owns the limit config",
            self.server_url, reason,
        )

    def _apply_server_limits(self, limits: dict) -> None:
        """Store the server's authoritative snapshot for the delegated backend.

        The server's GET returns config *and* live progress
        (`wh`/`mode`/`fired`/`session_wh`/`is_charging`), so the entities can
        mirror the Web UI exactly instead of showing a divergent local figure.
        Kept out of `_charge_limits` on purpose — see `_server_limits`.
        """
        for port in PORT_MAP:
            entry = limits.get(port)
            if isinstance(entry, dict):
                self._server_limits[port] = dict(entry)
        # Both callback lists: config feeds number/select, progress feeds the
        # session-energy sensor (CB_TYPE_PORT), which otherwise only refreshes
        # on MQTT samples.
        self._notify_callbacks(self._settings_callbacks)
        self._notify_callbacks(self._port_callbacks)

    @property
    def _delegated(self) -> bool:
        """True when the BLE server owns charge-limit config and enforcement."""
        return self._limit_backend == LIMIT_BACKEND_SERVER

    def _server_field(self, port: str, key: str):
        """Read one field from the delegated snapshot, or None if unavailable."""
        if not self._delegated:
            return None
        entry = self._server_limits.get(port)
        if not isinstance(entry, dict):
            return None
        return entry.get(key)

    def _server_number(self, port: str, key: str, fallback):
        """Delegated numeric field (clamped non-negative), else `fallback()`.

        `fallback` is a callable rather than a value so the local engine is only
        consulted when the server has nothing to say.
        """
        raw = self._server_field(port, key)
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass  # fall through to the local figure
        return fallback()

    def _server_bool(self, port: str, key: str, fallback) -> bool:
        """Delegated boolean field, else `fallback()`."""
        raw = self._server_field(port, key)
        return bool(raw) if raw is not None else fallback()

    def _adopt_server_limits_locally(self) -> None:
        """Take over server config locally (used when delegation breaks).

        Copies the last known server snapshot into the local engine so a
        fallback doesn't blank out the other ports' displayed limits, then
        stops polling: from now on HA owns the config.
        """
        for port in PORT_MAP:
            entry = self._server_limits.get(port)
            if isinstance(entry, dict):
                self._charge_limits.set_limit(port, entry.get("wh"), entry.get("mode"))
        if self._limit_poll_unsub:
            self._limit_poll_unsub()
            self._limit_poll_unsub = None
        self._limit_backend = LIMIT_BACKEND_LOCAL

    async def _async_poll_server_limits(self, _now) -> None:
        """Refresh delegated limits from the server (route B keep-sync)."""
        if not self._delegated:
            return
        limits, _reason = await self._async_limit_request()
        if limits is not None:
            self._apply_server_limits(limits)

    async def _async_post_limit(self, port: str, wh: float, mode=None) -> bool:
        """POST one port's limit to the server. Returns True on success."""
        body = {"port": port, "wh": wh}
        if mode:
            body["mode"] = mode
        limits, reason = await self._async_limit_request(body=body)
        if limits is None:
            _LOGGER.warning("Failed to POST charge limit for %s: %s", port, reason)
            return False
        self._apply_server_limits(limits)
        return True

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
        http_recent = (self.hass.loop.time() - self._last_status_time) < STATUS_STALE_SECONDS
        self._available = self._mqtt_connected or http_recent

    async def _async_health_check(self, _now) -> None:
        """Check if BLE server is reachable via HTTP."""
        session = async_get_clientsession(self.hass)
        was_available = self._available
        try:
            url = f"{self.server_url}/api/status"
            async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
                # body 必须在 async with 块内读取，连接退出上下文后即关闭，
                # 之后调用 resp.json()/resp.read() 会抛异常。
                if resp.status == 200:
                    self._last_status_time = self.hass.loop.time()
                    self._health_failures = 0
                    self._update_availability()
                    if self._available and not was_available:
                        _LOGGER.info("BLE server is now available (HTTP)")
                    # Fallback: also read connection status and device info from HTTP if MQTT not connected
                    if not self._mqtt_connected:
                        try:
                            data = await resp.json()
                        except Exception as err:
                            _LOGGER.warning(
                                "Failed to parse health check JSON response: %s", err
                            )
                        else:
                            await self._async_health_check_parse_body(data)
                else:
                    self._log_health_failure(
                        f"BLE server returned HTTP status {resp.status}"
                    )
                    self._available = self._mqtt_connected
                    await resp.read()  # 读完 body，确保连接完全复用/释放
        except Exception as err:
            self._log_health_failure("BLE server HTTP health check failed", str(err))
            self._available = self._mqtt_connected
        # 可用性翻转时通知实体刷新（实体 available 是动态属性，需要一次
        # write_ha_state 才会更新前端；仅在变化时广播，避免每 30s 全量刷新）
        if self._available != was_available:
            _LOGGER.debug("Availability changed via health check: %s -> %s",
                          was_available, self._available)
            self._notify_all()

    async def _async_health_check_parse_body(self, data: dict) -> None:
        """Parse health check JSON body (already parsed) and sync device info."""
        try:
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

    # --- Charge limit accessors (for number/select/sensor entities) ---
    # In delegated (`server`) mode the server is authoritative and its figures
    # are what the Web UI shows, so entities mirror them; the local engine is
    # still metering underneath and takes over seamlessly if delegation ends.

    def charge_limit_wh(self, port: str) -> float:
        """Configured limit Wh for a port (0 = disabled)."""
        return self._server_number(
            port, "wh", lambda: self._charge_limits.get_limit(port).wh)

    def charge_limit_mode(self, port: str) -> str:
        """Configured mode for a port (once/always)."""
        raw = self._server_field(port, "mode")
        if raw in LIMIT_MODES:
            return raw
        return self._charge_limits.get_limit(port).mode

    def charge_limit_remaining_wh(self, port: str) -> float | None:
        """Remaining Wh before cutoff, None when no limit is armed."""
        limit = self.charge_limit_wh(port)
        if limit <= 0:
            return None
        return round(max(0.0, limit - self.session_energy_wh(port)), 3)

    def charge_limit_fired(self, port: str) -> bool:
        """True while a cutoff for the current session is in flight."""
        return self._server_bool(
            port, "fired", lambda: self._charge_limits.has_fired(port))

    def charge_limit_charging(self, port: str) -> bool:
        """True when the active backend considers the port mid-session."""
        return self._server_bool(
            port, "is_charging", lambda: self._charge_limits.is_charging(port))

    def session_energy_wh(self, port: str) -> float:
        """Energy delivered in the current session, rounded for display.

        Prefers the server value when delegated: it is the same figure the Web
        UI shows and it survives HA restarts (our local accumulator cannot,
        since nothing samples while HA is down).
        """
        return round(self._server_number(
            port, "session_wh", lambda: self._charge_limits.session_wh(port)), 3)

    def total_energy_wh(self, port: str) -> float:
        """Energy accumulated since last HA restart / store reset."""
        return round(self._charge_limits.total_wh(port), 3)

    def session_max_power(self, port: str) -> float:
        """Peak power observed in the current session."""
        return round(self._charge_limits.max_power(port), 2)

    # --- BLE control ---

    async def async_enable_ble(self, enable: bool) -> bool:
        """Enable or disable BLE connection via MQTT (primary) + HTTP (fallback)."""
        async with self._ble_lock:
            # Cancel any previous pending timeout
            if self._ble_timeout_task is not None and not self._ble_timeout_task.done():
                self._ble_timeout_task.cancel()
                self._ble_timeout_task = None

            prev_enabled = self._ble_enabled
            self._ble_enabled = enable
            self._ble_pending = True
            self._notify_callbacks(self._callbacks)

            async def _clear_pending_after_delay() -> None:
                await asyncio.sleep(BLE_OPERATION_TIMEOUT)
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
                    async with session.post(url, json={"enabled": enable}, timeout=BLE_OPERATION_TIMEOUT) as resp:
                        if resp.status == 200:
                            _LOGGER.info("BLE connection %s via HTTP (fallback)", "enabled" if enable else "disabled")
                            success = True
                except Exception as err:
                    _LOGGER.warning("HTTP BLE control also failed: %s", err)

            self._ble_pending = False
            if self._ble_timeout_task is not None and not self._ble_timeout_task.done():
                self._ble_timeout_task.cancel()
            self._ble_timeout_task = None
            if not success:
                # 两个通道都失败: 回滚到操作前的状态，避免开关显示"已开"
                # 但实际未建立连接且无纠正来源 (设备离线时不会推送 status 纠正)
                self._ble_enabled = prev_enabled
                _LOGGER.warning(
                    "BLE %s failed on both MQTT and HTTP, reverting switch state",
                    "enable" if enable else "disable",
                )
            self._notify_callbacks(self._callbacks)
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
            value = encode_protocol_switches(switches)
            await self.async_set_value(21, value)
