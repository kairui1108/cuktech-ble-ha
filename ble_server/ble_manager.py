"""CUKTECH BLE Server - BLE connection manager with auto-reconnect."""
import asyncio
import logging
import sys
import os
import random
import threading
import time
from datetime import datetime, timezone

try:
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS

from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT, decode_port, decode_pdo_caps

_LOGGER = logging.getLogger("cuktech_ble")

_status_cache_invalidator = None


def set_status_cache_invalidator(invalidator):
    global _status_cache_invalidator
    _status_cache_invalidator = invalidator


def _invalidate():
    if _status_cache_invalidator:
        _status_cache_invalidator()


def _has_bluetoothctl():
    """Check if bluetoothctl is available."""
    import shutil
    return shutil.which("bluetoothctl") is not None


class BLEManager:
    # ── Concurrency & recovery limits ──
    CMD_QUEUE_MAXSIZE = 500      # prevent unbounded command growth
    RECONNECT_JITTER = 0.25      # ±25% jitter on reconnect delays
    CIRCUIT_BREAKER_MAX_FAIL = 20  # consecutive failures before cooling off
    CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes

    def __init__(self, mac, token, state, config):
        self.mac = mac
        self.token = bytes.fromhex(token)
        self.state = state
        self.config = config
        self.ctrl = None
        self.cmd_queue = asyncio.Queue(maxsize=self.CMD_QUEUE_MAXSIZE)
        self._stop_event = asyncio.Event()
        self._port_timer_task = None
        self._mqtt_publish = None
        self._sse_emitter = None
        self._quality_provider = None
        # 充电会话记录开关：关闭时只停止 DB 写入（start/point/end 均 gate），
        # 内存会话生命周期/实时显示/充电完成事件照常。启动时由服务器从 DB meta 注入。
        self.record_sessions: bool = True
        # 记录关闭期间用于占位的内存会话 id（负值递减，不与真实 DB id 冲突）
        self._fake_sid_counter: int = 0
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._total_frames = 0
        self._last_notify_time = 0.0
        self._reconnect_times = []  # timestamps of recent reconnects
        self._keepalive_fails = 0
        self._auth_fail_count = 0
        self._ble_connect_time = 0.0  # timestamp of current BLE connection
        self._base_reconnect_delay = config.server.reconnect_base_delay
        self._max_reconnect_delay = config.server.reconnect_max_delay
        self._history = None
        self._sess_lock = threading.Lock()  # protects _active_sessions (accessed only from event loop)
        self._circuit_breaker_cooldown = 0.0
        self._circuit_breaker_failures = 0
        # Energy tracking
        from energy import AdaptiveEnergyIntegrator, PortEnergyState, ChargeEndDetector
        self._energy_integrator = AdaptiveEnergyIntegrator()
        self._energy_states = {i: PortEnergyState() for i in range(1, 5)}
        self._charge_detectors = {i: ChargeEndDetector() for i in range(1, 5)}
        self._active_sessions = {}  # port -> session_id
        # Protocol debounce: track consecutive protocol readings per port
        self._proto_buf = {i: [] for i in range(1, 5)}  # port -> [last N protocols]
        self._PROTO_DEBOUNCE_N = 3  # consecutive readings to confirm protocol
        # Session end debounce: consecutive low-current count per port
        self._low_current_count = {i: 0 for i in range(1, 5)}
        self._LOW_CURRENT_N = 300  # consecutive readings below threshold to end session
        # Idle port verification: when a port stops receiving BLE pushes, actively
        # GET it after IDLE_VERIFY_SEC to detect unplug (V/I stuck at last value).
        self._IDLE_VERIFY_SEC = 15          # idle > 15s → trigger one active GET
        self._last_verify_time = {i: 0.0 for i in range(1, 5)}
        self._pending_verify = set()        # ports queued for verify (avoids duplicate enqueue)

    def set_mqtt_publisher(self, publisher):
        self._mqtt_publish = publisher

    def set_sse_emitter(self, emitter):
        self._sse_emitter = emitter

    def set_quality_provider(self, provider):
        """Set a callback that returns combined quality dict from all sources."""
        self._quality_provider = provider

    def _sse_emit(self, event_type, data):
        """Emit SSE event if emitter is connected. Sync — emitter uses threading.Lock internally."""
        if self._sse_emitter:
            self._sse_emitter.emit(event_type, data)

    def set_history(self, history):
        self._history = history

    @property
    def is_running(self) -> bool:
        """是否正在运行 (不处于停止状态)。"""
        return not self._stop_event.is_set()

    def connection_quality(self) -> dict:
        """Estimate BLE connection quality (0-100) from available metrics."""
        total = self._total_frames or 1
        # 1. Decrypt success rate (40%)
        decrypt_score = max(0, ((total - self._decrypt_failures) / total) * 100)
        # 2. Notification responsiveness — time since last BLE push (30%)
        notify_age = time.time() - self._last_notify_time if self._last_notify_time else 999
        notify_score = max(0, min(100, 100 - notify_age * 10))
        # 3. Reconnect frequency in last 5 min (20%)
        recent = sum(1 for t in self._reconnect_times if time.time() - t < 300)
        reconnect_score = max(0, 100 - recent * 25)
        # 4. Keepalive success (10%)
        keepalive_fails = self._keepalive_fails
        keepalive_score = max(0, 100 - keepalive_fails * 33)
        score = round(decrypt_score * 0.4 + notify_score * 0.3 +
                      reconnect_score * 0.2 + keepalive_score * 0.1)
        # Connection uptime
        uptime = int(time.time() - self._ble_connect_time) if self._ble_connect_time else 0
        # Last push age
        last_push_age = round(time.time() - self._last_notify_time) if self._last_notify_time else None
        # Next reconnect delay (when disconnected)
        next_delay = self._get_reconnect_delay() if self._reconnect_attempts > 0 else None
        return {
            "score": score,
            "decrypt": round(decrypt_score),
            "notify": round(notify_score),
            "reconnect_score": round(reconnect_score),
            "reconnect_count_5m": recent,
            "keepalive": round(keepalive_score),
            "total_frames": total,
            "decrypt_failures": self._decrypt_failures,
            "uptime": uptime,
            "last_push_age": last_push_age,
            "next_reconnect_delay": next_delay,
        }

    def get_live_session_data(self) -> dict:
        """Get real-time energy data for active charging sessions.
        Returns dict mapping port (1-4) to {session_id, session_wh, max_power, start_time}.
        """
        result = {}
        for port, es in self._energy_states.items():
            if es.is_charging and port in self._active_sessions:
                result[port] = {
                    "session_id": self._active_sessions[port],
                    "session_wh": round(es.session_wh, 4),
                    "max_power": round(es.max_power, 2),
                    "start_time": es.session_start,
                }
        return result

    async def request_stop(self):
        """请求停止 BLE 循环 (设置 _stop_event，不直接断开)。"""
        self._close_active_sessions()
        self._stop_event.set()

    def _close_active_sessions(self):
        """Gracefully close all active charge sessions on shutdown.

        事件（MQTT 充电完成）始终发布；DB 记录仅当真实会话（正 sid）且记录
        开关开启时写入。
        """
        now = time.time()
        for port, es in self._energy_states.items():
            if es.is_charging:
                with self._sess_lock:
                    sid = self._active_sessions.pop(port, None)
                duration = int(now - (es.session_start or now))
                det = self._charge_detectors[port]
                det.on_session_end(now)
                es.is_charging = False
                es.last_end_time = now
                db_sid = sid if (sid and sid > 0) else 0
                if es.session_wh >= 0.05:
                    ps = self.state.ports.get(port)
                    if ps:
                        self._publish_charge_event(
                            port, db_sid, es, now,
                            ps.voltage, ps.current, duration)
                    _LOGGER.info("Closing session %s (port %d, %.1fWh, %ds)",
                                 sid if sid else "n/a", port, es.session_wh, duration)
                    if db_sid and self._history and self.record_sessions:
                        self._history.end_session(sid, es.session_wh, es.max_power, 0, 0, duration)

    def resume_recording_sessions(self) -> None:
        """打开记录开关时调用：把关闭期间仍在充电的占位会话立即转为真实记录。

        对每个持有占位（负）sid 的端口：异步补建 DB 会话（start_session）并
        从打开时刻起重新累计该端口能量（丢置关闭期间的积分，与"关闭不记录"
        语义一致）。此后该充电会话的曲线/历史立即有数据，无需等到下次充电。
        注意：必须在事件循环内调用。
        """
        if not self._history:
            return
        loop = asyncio.get_running_loop()
        now = time.time()
        for port, sid in list(self._active_sessions.items()):
            if sid >= 0:
                continue  # 已是真实会话（记录开启时创建）
            es = self._energy_states.get(port)
            if not es or not es.is_charging:
                continue
            ps = self.state.ports.get(port)
            protocol = (ps.protocol if ps else "") or ""
            # 从打开时刻重新开始记录：重置会话起点、能量累计与峰值
            # （峰值不保留切换前的值，避免污染转正会话的统计）
            es.session_start = now
            es.session_wh = 0.0
            es.max_power = 0.0
            es.max_current = 0.0

            task = loop.run_in_executor(None, self._history.start_session, port, protocol)

            def _on_upgrade(t, p=port, fake=sid):
                """转正回调：若窗口内会话已结束或被新会话替换，立即闭合刚建的
                DB 会话行（start_session 已完成，避免孤儿行）；否则写入真 sid。"""
                if t.exception():
                    _LOGGER.error("Resume recording session failed for port %d: %s", p, t.exception())
                    return
                new_sid = t.result()
                if not new_sid:
                    _LOGGER.error("Resume recording session failed for port %d: DB returned no sid", p)
                    return
                with self._sess_lock:
                    es2 = self._energy_states.get(p)
                    if es2 is None or not es2.is_charging or self._active_sessions.get(p) != fake:
                        # 转正窗口内会话已结束/被替换：闭合刚建的 DB 行
                        self._close_resumed_orphan(p, new_sid)
                        return
                    self._active_sessions[p] = new_sid
                _LOGGER.info("Session recording resumed for port %d (sid=%s)", p, new_sid)

            task.add_done_callback(_on_upgrade)

    def _close_resumed_orphan(self, port: int, session_id: int) -> None:
        """闭合转正期间被过早创建的 DB 会话行（会话在 start_session 完成前已结束）。"""
        if not self._history:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning(
                "_close_resumed_orphan: no event loop, session %d left open for port %d",
                session_id, port)
            return
        task = loop.run_in_executor(None, self._history.delete_session, session_id)
        task.add_done_callback(
            lambda t, _sid=session_id: _LOGGER.error(
                "Close resumed orphan session %d failed: %s", _sid, t.exception())
            if t.exception() else None)

    def _record_charge_point(self, piid: int, voltage: float, current: float,
                             protocol: str = "") -> bool:
        """写入会话采样点（仅真实会话且记录开启时；占位负 sid 或记录关闭均跳过）。

        集中两处采样点写入门控，返回是否实际提交了写库任务。
        """
        if not self.record_sessions:
            return False
        sid = self._active_sessions.get(piid)
        if not sid or sid <= 0:
            return False
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(
            None, self._history.record_charge_point,
            sid, voltage, current, round(voltage * current, 1), protocol)
        task.add_done_callback(
            lambda t: _LOGGER.error("Record charge point failed: %s", t.exception()) if t.exception() else None)
        return True

    def _close_session(self, piid, timestamp, voltage=0, current=0):
        """Close a charge session: cleanup state, notify, and write to DB.

        会话记录开关（record_sessions）只影响 DB 写入：关闭记录期间创建的会话
        使用内存占位 sid（负值），实时显示与充电完成事件（MQTT，存在有效能量
        >=0.05Wh 时）照常；仅真实会话（开启期间创建的正 sid）且开关开启时才落库。
        重开开关后正在充电的会话保持显示（占位 sid 仍在 _active_sessions）。
        """
        det = self._charge_detectors[piid]
        es = self._energy_states[piid]
        with self._sess_lock:
            sid = self._active_sessions.pop(piid, None)
        if sid is None and not es.is_charging:
            return None
        det.on_session_end(timestamp)
        es.is_charging = False
        es.last_end_time = timestamp
        duration = int(timestamp - (es.session_start or timestamp))
        # 仅真实会话（开启记录期间创建的正 sid）可以写库；负 sid 为关闭期间占位
        db_sid = sid if (sid and sid > 0) else 0

        if es.session_wh < 0.05:
            # 无有效能量（如瞬时插拔）：不发事件、不写库（恢复旧语义）；
            # 真实会话（正 sid）需清理其占用的 DB 会话行
            if db_sid and self._history and self.record_sessions:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    _LOGGER.warning("_close_session: no event loop, skip DB delete for session %d", sid)
                    return sid
                task = loop.run_in_executor(
                    None, self._history.delete_session, sid)
                task.add_done_callback(
                    lambda t, _sid=sid: _LOGGER.error("Close session %d failed: %s", _sid, t.exception()) if t.exception() else None)
            else:
                _LOGGER.info("Charge session ended without recording (port %d, %.1fWh, %ds)",
                             piid, es.session_wh, duration)
            return sid

        # 充电完成事件：仅在存在有效能量（>=0.05Wh）时发布，记录开关不影响
        # HA 通知；未落库会话（recorded=False）session_id 用 0 表示。
        self._publish_charge_event(piid, db_sid, es, timestamp,
                                   voltage, current, duration)

        if not db_sid:
            # 记录关闭期间的会话（占位 sid）：事件已发，不写库、不发 SSE
            _LOGGER.info("Charge session ended without DB recording (port %d, %.1fWh, %ds)",
                         piid, es.session_wh, duration)
            return sid

        # Emit SSE session_end event for real-time UI update
        self._sse_emit("session_end", {
            "session_id": sid,
            "port": PORT_NAMES.get(piid, str(piid)),
            "port_id": piid,
            "total_wh": round(es.session_wh, 4),
            "peak_power_w": round(es.max_power, 2),
            "duration_sec": duration,
        })
        if self._history and self.record_sessions:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _LOGGER.warning("_close_session: no event loop, skip DB write for session %d", sid)
                return sid
            task = loop.run_in_executor(
                None, self._history.end_session, sid,
                round(es.session_wh, 4), round(es.max_power, 2),
                round(voltage, 2), round(current, 2), duration)
            task.add_done_callback(
                lambda t, _sid=sid: _LOGGER.error("Close session %d failed: %s", _sid, t.exception()) if t.exception() else None)
        return sid

    def _publish_charge_event(self, piid, sid, es, timestamp, voltage, current, duration):
        """Publish charge completion event via MQTT.

        sid 约定：0 表示会话未落库（记录关闭 / DB 启动失败），此时 recorded=False；
        正值为真实 DB 会话 id（recorded=True）。
        """
        if not self._mqtt_publish:
            return
        try:
            ps = self.state.ports.get(piid)
            payload = {
                "event": "charge_end",
                "port": PORT_NAMES.get(piid, str(piid)),
                "port_id": piid,
                "session_id": sid,
                "recorded": bool(sid),
                "start_time": datetime.fromtimestamp(es.session_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if es.session_start else None,
                "end_time": datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": duration,
                "energy_wh": round(es.session_wh, 4),
                "avg_power_w": round(es.session_wh / (duration / 3600), 1) if duration > 0 else 0,
                "max_power_w": round(es.max_power, 2),
                "protocol": (ps.protocol if ps else "") or "idle",
                "voltage": round(voltage, 2) if voltage else round(ps.voltage, 2) if ps else 0,
                "current": round(current, 2) if current else round(ps.current, 2) if ps else 0,
            }
            self._mqtt_publish(self.config.topic_charge_event, payload)
            _LOGGER.info("Charge event published: port=%s energy=%.1fWh duration=%ds",
                         PORT_NAMES.get(piid, str(piid)), es.session_wh, duration)
        except Exception as err:
            _LOGGER.error("Failed to publish charge event: %s", err)

    def _get_reconnect_delay(self):
        """Calculate exponential backoff delay with jitter."""
        delay = min(
            self._base_reconnect_delay * (2 ** min(self._reconnect_attempts, 10)),
            self._max_reconnect_delay
        )
        # Add jitter (±25%) to prevent thundering herd
        if delay > 1.0:
            jitter = delay * self.RECONNECT_JITTER * (random.random() * 2 - 1)
            delay += jitter
        return max(0.5, delay)

    def _check_circuit_breaker(self) -> bool:
        """Return True if the circuit breaker is open (should NOT connect)."""
        now = time.time()
        if self._circuit_breaker_cooldown > 0:
            if now < self._circuit_breaker_cooldown:
                return True
            # Cooldown expired
            self._circuit_breaker_cooldown = 0.0
            self._circuit_breaker_failures = 0
        return False

    def _record_circuit_breaker_failure(self):
        """Record a failure; trip circuit breaker if threshold reached."""
        self._circuit_breaker_failures += 1
        if self._circuit_breaker_failures >= self.CIRCUIT_BREAKER_MAX_FAIL:
            _LOGGER.warning(
                "Circuit breaker tripped (%d failures), cooling off for %ds",
                self._circuit_breaker_failures, self.CIRCUIT_BREAKER_COOLDOWN,
            )
            self._circuit_breaker_cooldown = time.time() + self.CIRCUIT_BREAKER_COOLDOWN

    async def start(self):
        self._stop_event.clear()
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._auth_fail_count = 0
        first_run = True
        last_error = None
        while not self._stop_event.is_set():
            # Check circuit breaker before attempting connection
            if self._check_circuit_breaker():
                remaining = int(self._circuit_breaker_cooldown - time.time())
                _LOGGER.warning(
                    "Circuit breaker open, waiting %ds before next attempt",
                    max(remaining, 0),
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=max(remaining, 30),
                    )
                    break
                except asyncio.TimeoutError:
                    continue

            try:
                await self._connect_and_run()
                self._reconnect_attempts = 0
                self._decrypt_failures = 0
                self._auth_fail_count = 0
                self._circuit_breaker_failures = 0
                first_run = False
                last_error = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                last_error = e
                self._record_circuit_breaker_failure()
                err_str = str(e)
                if 'POWERED_OFF' in err_str or 'No powered Bluetooth' in err_str:
                    _LOGGER.warning("Bluetooth is powered off, will retry in 60s...")
                elif 'Charger not found' in err_str or 'BLE scan failed' in err_str:
                    # 可恢复的扫描错误，不需要完整堆栈
                    _LOGGER.warning("BLE loop error: %s (retry %d)", e, self._reconnect_attempts + 1)
                else:
                    _LOGGER.error("BLE loop error: %s", e, exc_info=True)
            finally:
                await self._disconnect()
            if not self._stop_event.is_set():
                if isinstance(last_error, AuthConnectionError):
                    # auth 失败可能有两类原因:
                    # 1. 设备端 session 未清除 (需等待设备自然超时)
                    # 2. BlueZ GATT 缓存损坏 (需 power cycle 本地适配器)
                    # 因此 auth 失败也应重置本地适配器，避免陷入永久失败
                    self._reconnect_attempts = 0  # reset: auth failure has its own counter
                    self._auth_fail_count += 1
                    await self._force_disconnect_bluetooth()
                    if self._auth_fail_count >= 5:
                        _LOGGER.error(
                            "Auth failed %d times consecutively. "
                            "Device session is stuck. Please power-cycle the charger "
                            "(unplug and replug) to reset its BLE session.",
                            self._auth_fail_count)
                        self._publish_status({"connected": False, "error": "device_session_stuck"}, retain=True)
                        # 等待 5 分钟后自动重试（给用户时间手动重启）
                        delay = 300
                    else:
                        delay = min(60 * self._auth_fail_count, 180)
                    _LOGGER.warning("Auth failed %d times, reset adapter and waiting %ds...",
                                    self._auth_fail_count, delay)
                elif last_error and ('POWERED_OFF' in str(last_error) or 'No powered Bluetooth' in str(last_error)):
                    delay = 60  # Bluetooth powered off, check less frequently
                elif last_error and 'Charger not found' in str(last_error):
                    # 充电器不在范围内: 不需要 power cycle 适配器，只重试扫描
                    delay = self._get_reconnect_delay()
                elif last_error:
                    await self._force_disconnect_bluetooth()
                    delay = self._get_reconnect_delay()
                else:
                    delay = self._get_reconnect_delay()
                self._reconnect_attempts += 1
                self._reconnect_times.append(time.time())
                # Prune to last 10 minutes
                cutoff = time.time() - 600
                self._reconnect_times = [t for t in self._reconnect_times if t > cutoff]
                if 'Charger not found' in str(last_error or ''):
                    _LOGGER.info("Waiting %.0fs before retry (attempt %d)...", delay, self._reconnect_attempts)
                elif 'POWERED_OFF' not in str(last_error or '') and 'No powered Bluetooth' not in str(last_error or ''):
                    _LOGGER.info("Reconnecting in %.0fs (attempt %d)...", delay, self._reconnect_attempts)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

    async def stop(self):
        self._close_active_sessions()
        self._stop_event.set()
        await self._disconnect()
        if _has_bluetoothctl():
            await self._force_disconnect_bluetooth()

    def _find_ble_adapter(self):
        """自动检测支持 BLE 的蓝牙适配器名称（如 hci0, hci1）"""
        if not os.path.exists("/sys/class/bluetooth"):
            return "hci0"
        import glob
        hci_devs = sorted(glob.glob("/sys/class/bluetooth/hci*"))
        for hci_dir in hci_devs:
            hci_name = os.path.basename(hci_dir)
            if ":" in hci_name:
                continue
            if os.path.isdir(os.path.join(hci_dir, "device")):
                return hci_name
        return "hci0"

    async def _connect(self):
        _LOGGER.info("Scanning for charger...")

        # 先清理 BlueZ 残留扫描状态（避免 InProgress 错误）
        await self._stop_ble_scan()

        from bleak import BleakScanner
        try:
            found = await BleakScanner.find_device_by_address(
                self.mac, timeout=self.config.ble.scan_timeout)
        except Exception as e:
            err_str = str(e)
            _LOGGER.error("BLE scan failed: %s", e)
            # InProgress 错误 → 适配器状态异常，需要 power cycle
            if 'InProgress' in err_str:
                await self._force_disconnect_bluetooth()
            raise ConnectionError(f"BLE scan failed: {e}")
        if not found:
            _LOGGER.warning("Charger not found with MAC: %s (will retry)", self.mac)
            raise ConnectionError("Charger not found")

        self.ctrl = CuktechBLEController(self.mac, self.token)
        await self.ctrl.connect()

        _LOGGER.info("Connected, waiting for device to settle...")
        await asyncio.sleep(2)

        await self.ctrl.read_device_info()
        _LOGGER.info("Connected, authenticating...")
        # 存储设备信息到 state
        await self.state.update_device_info(self.ctrl.device_model, self.ctrl.firmware_version)

        if not await self.ctrl.authenticate():
            _LOGGER.warning("Auth failed, disconnecting BLE...")
            try:
                if self.ctrl.client and self.ctrl.client.is_connected:
                    await self.ctrl.stop_all_notifications()
                    await self.ctrl.client.disconnect()
            except Exception:
                pass
            # 等待设备处理断连，避免旧连接未完全释放时新连接冲突
            await asyncio.sleep(3)
            raise AuthConnectionError("Auth failed")

        self._auth_fail_count = 0  # reset on successful auth
        self._ble_connect_time = time.time()
        self._last_notify_time = 0.0  # reset so quality shows "无" until first push
        await self.state.set_connection(True, True)
        _invalidate()
        _LOGGER.info("Authenticated!")

        # 立即推送连接状态，前端无需等待 15 个 PIID 读完
        self._publish_status({"connected": True, "authenticated": True}, retain=True)

        # 先读取 PIID 17 (c1_c2_protocol) 获取硬件协议代码
        # 再处理 init_push 端口数据，确保 hw_protocol 已就绪
        await self._read_initial_settings()

        # 处理认证后设备推送的初始端口数据
        if self.ctrl.init_push_frames:
            _LOGGER.info("Processing %d init push frames", len(self.ctrl.init_push_frames))
            for frame in self.ctrl.init_push_frames:
                await self._try_process_inline_frame(frame)
            self.ctrl.init_push_frames = []

        # Build full state for status event
        ports_data = {}
        port_ctl = self.state.settings.get("16", 0x0F)
        for piid, pname in PORT_NAMES.items():
            ps = self.state.ports.get(piid)
            port_data = ps.to_dict() if ps else dict(PORT_DEFAULT)
            port_data["enabled"] = bool(port_ctl & (1 << (piid - 1)))
            ports_data[str(piid)] = port_data
        self._publish_status({
            "connected": True,
            "authenticated": True,
            "device_model": self.ctrl.device_model,
            "firmware_version": self.ctrl.firmware_version,
            "ports": ports_data,
            "settings": self.state.settings,
            "protocol_switches": self.state.protocol_switches,
            "protocol_extend": self.state.protocol_extend,
        }, retain=True)

        await asyncio.sleep(2)

    async def _disconnect(self):
        if self.ctrl:
            client = self.ctrl.client if self.ctrl else None
            was_connected = bool(client and client.is_connected)
            # 始终进行 GATT cleanup，确保设备收到干净的 BLE LL disconnect
            # （无论是否 stop，设备端都需要感知断开以清除 auth session）
            try:
                if client and client.is_connected:
                    await self.ctrl.stop_all_notifications()
            except Exception:
                pass
            try:
                if client and client.is_connected:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=3.0)
                    except Exception:
                        pass
            except Exception:
                pass
            self.ctrl = None
            self._ble_connect_time = 0.0  # reset uptime on disconnect
            self._last_notify_time = 0.0  # reset push tracking on disconnect
            self._total_frames = 0       # reset frame counter on disconnect
            # Close active charge sessions on disconnect
            self._close_active_sessions()
            if was_connected and not self._stop_event.is_set():
                _LOGGER.error("BLE device disconnected unexpectedly")
        await self.state.set_connection(False, False)
        _invalidate()
        self._publish_status({
            "connected": False,
            "device_model": self.state.device_model,
            "firmware_version": self.state.firmware_version,
        }, retain=True)
        # bluetoothctl disconnect MAC 由 _force_disconnect_bluetooth() 统一处理
        # 此处不再重复调用，避免设备收到多次断连通知导致状态混乱

    async def _stop_ble_scan(self):
        """Cancel any lingering BLE scan on the adapter before starting a new one.
        Prevents [org.bluez.Error.InProgress] Operation already in progress.
        """
        if not _has_bluetoothctl():
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "scan", "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass

    async def _force_disconnect_bluetooth(self):
        """使用 bluetoothctl 强制断开蓝牙连接并重置适配器。

        仅在 Linux + bluetoothctl 可用时执行；其它平台由 bleak 层处理断连，
        适配器电源循环属于 Linux 特有的 BlueZ 恢复手段，跳过不影响功能。
        """
        if not _has_bluetoothctl():
            _LOGGER.info("bluetoothctl not available, skipping adapter power cycle")
            return
        # 先停止残留扫描，再断开已有连接，最后重置适配器
        await self._stop_ble_scan()
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", self.mac,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待 BLE Link Layer disconnect 完成
            await asyncio.sleep(1)
        except Exception as e:
            _LOGGER.warning("bluetoothctl disconnect failed: %s", e)
        # 重置蓝牙适配器以清理残留状态
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "power", "off",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            await asyncio.sleep(1)
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "power", "on",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待适配器就绪，最多15秒
            hci = self._find_ble_adapter()
            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "bluetoothctl", "show", hci,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                    if b"Powered: yes" in stdout:
                        _LOGGER.info("BT adapter ready after power cycle")
                        break
                except Exception:
                    try:
                        power_file = f"/sys/class/bluetooth/{hci}/power"
                        if os.path.exists(power_file):
                            with open(power_file) as f:
                                if f.read().strip() == "1":
                                    _LOGGER.info("BT adapter ready (via sysfs)")
                                    break
                    except Exception:
                        pass
            else:
                _LOGGER.warning("BT adapter not ready after 15s, proceeding anyway")
        except Exception as e:
            _LOGGER.warning("bluetoothctl power cycle failed: %s", e)

    async def _connect_and_run(self):
        await self._connect()
        self._keepalive_fails = 0
        last_refresh = time.time()
        last_notify = time.time()
        last_keepalive = time.time()

        # Start 1-second background timer for port_history + energy accumulation
        self._port_timer_task = asyncio.ensure_future(self._port_timer())

        try:
            while not self._stop_event.is_set():
                await self._process_commands()

                if not self.ctrl:
                    break

                try:
                    data = await asyncio.wait_for(
                        self.ctrl.wait_notify("cmd_recv"), timeout=2.0)
                    if not self.ctrl:
                        break
                    last_notify = time.time()
                    self._last_notify_time = last_notify
                    self._total_frames += 1
                except asyncio.TimeoutError:
                    now = time.time()
                    # Refresh settings during idle (no BLE push — avoids data loss from drain)
                    if now - last_refresh > self.config.server.settings_refresh_interval:
                        await self._refresh_settings()
                        now = time.time()
                        last_refresh = now
                        last_notify = now
                    if now - last_keepalive > 10:
                        if self.ctrl and self.ctrl.client and self.ctrl.client.is_connected:
                            try:
                                # NOTE: must stay response=False — this device's
                                # characteristics are all Write-Without-Response,
                                # so response=True would time out on a healthy link
                                # and cause false reconnects. Link loss is instead
                                # detected via the disconnect callback below.
                                await self.ctrl.client.write_gatt_char(
                                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x00, 0x00]), response=False)
                                last_keepalive = now
                                self._keepalive_fails = 0
                            except Exception:
                                self._keepalive_fails += 1
                                if self._keepalive_fails >= 3:
                                    _LOGGER.warning("Keepalive failed 3 times, reconnecting")
                                    raise ConnectionError("BLE keepalive failed")
                    if now - last_notify > 60:
                        client = self.ctrl.client if self.ctrl else None
                        # Detect silent link loss. Bleak 3.x has no disconnect
                        # callback and is_connected may lag, so do an active probe:
                        # read the firmware version characteristic — a real BLE read
                        # fails once the link is actually gone.
                        lost = not client or not client.is_connected
                        if not lost:
                            try:
                                await asyncio.wait_for(
                                    client.read_gatt_char(CHAR_FW_VERSION), timeout=3.0)
                            except Exception:
                                lost = True
                        if lost:
                            _LOGGER.warning("BLE connection lost (is_connected=%s), triggering reconnect",
                                            client.is_connected if client else None)
                            raise ConnectionError("BLE disconnected")
                    continue
                except Exception as e:
                    _LOGGER.warning("BLE notification error: %s", e)
                    raise

                if not data or len(data) < 4:
                    continue

                if data[2] == 0x02 and len(data) >= 4:
                    await self._handle_inline_data(data)
                elif data[2] == 0x00 and len(data) >= 6:
                    await self._handle_multiframe(data)
                else:
                    await self._try_process_inline_frame(data)
        finally:
            if self._port_timer_task:
                self._port_timer_task.cancel()
                try:
                    await self._port_timer_task
                except asyncio.CancelledError:
                    pass

    async def _port_timer(self):
        """1-second timer: write port_history + energy + charge_points for ports
        that are NOT receiving BLE pushes (stable V/I). Also emits connection quality every 5s."""
        quality_tick = 0
        while not self._stop_event.is_set():
            await asyncio.sleep(1)
            # Emit connection quality every 5s (independent of history)
            quality_tick += 1
            if quality_tick % 5 == 0:
                q = self._quality_provider() if self._quality_provider else {"ble": self.connection_quality()}
                self._sse_emit("quality", q)
            if not self._history or self._stop_event.is_set():
                continue
            now = time.time()
            loop = asyncio.get_running_loop()
            for piid in range(1, 5):
                ps = self.state.ports.get(piid)
                if not ps or (ps.voltage <= 0 and ps.current <= 0):
                    continue
                es = self._energy_states[piid]
                # BLE handler already recorded if last_time < 2s ago
                idle = es.last_time is None or (now - es.last_time > 2)
                # Active verification: if the port has been idle (no BLE push)
                # longer than IDLE_VERIFY_SEC, enqueue a GET so the main loop
                # actively re-samples it. This catches the case where the unplug
                # push was lost (we were busy in an active GET) and ps stays at
                # the stale pre-unplug V/I forever.
                if (idle and es.last_time is not None
                        and now - es.last_time > self._IDLE_VERIFY_SEC
                        and now - self._last_verify_time[piid] > self._IDLE_VERIFY_SEC
                        and piid not in self._pending_verify):
                    self._pending_verify.add(piid)
                    try:
                        self.cmd_queue.put_nowait(("verify_port", piid, None))
                        self._last_verify_time[piid] = now
                    except asyncio.QueueFull:
                        self._pending_verify.discard(piid)
                if idle:
                    # Only integrate if current > 0 (no power transfer at 0A)
                    if es.is_charging and ps.current > 0:
                        self._energy_integrator.update(
                            es, ps.voltage, ps.current, now)
                        det = self._charge_detectors[piid]
                        det.update(ps.voltage * ps.current, now)
                        # Check if session should end (gradual power decline)
                        if det.should_end_session(es, now):
                            self._low_current_count[piid] = 0
                            sid = self._close_session(piid, now, ps.voltage, ps.current)
                            if sid and sid > 0:
                                _LOGGER.info("Timer ended session %d (port %d, %.1fWh)",
                                             sid, piid, es.session_wh)
                        else:
                            # 仅真实会话（正 sid）且记录开启时写入采样点
                            self._record_charge_point(
                                piid, ps.voltage, ps.current, ps.protocol or "")
                    # port_history: always write for chart continuity
                    task = loop.run_in_executor(
                        None, self._history.record_port_data,
                        piid, ps.to_dict())
                    task.add_done_callback(
                        lambda t: _LOGGER.error("Timer record_port_data failed: %s", t.exception()) if t.exception() else None)

    async def _fetch_settings(self, update_existing=False):
        settings = dict(self.state.settings) if update_existing else {}
        pdo_caps = {}
        fail_count = 0
        for piid in READABLE_SETTINGS_PIIDS:
            try:
                result = await self.ctrl.send_miot_command(2, piid)
                if result and "value" in result:
                    settings[str(piid)] = result["value"]
                    if piid == 17:
                        pdo_caps["c1c2"] = decode_pdo_caps(result["value"], "c1", "c2")
                        # PIID 17 byte[0]=C1 协议代码, byte[2]=C2 协议代码
                        # 与米家 parseC1C2ProtocolInfo 一致
                        val32 = result["value"] & 0xFFFFFFFF
                        c1_proto = (val32 >> 24) & 0xFF
                        c2_proto = (val32 >> 8) & 0xFF
                        # 零值保护在 state 层自动处理
                        await self.state.set_hw_protocol_codes(c1_proto, c2_proto)
                        _LOGGER.info("PIID17 hw_protocol_codes: C1=%d C2=%d (raw=0x%08X)",
                                     self.state._hw_protocol_c1, self.state._hw_protocol_c2, val32)
                    elif piid == 18:
                        pdo_caps["c3a"] = decode_pdo_caps(result["value"], "c3", "a")
                        # PIID 18 byte[0]=C3 协议代码, byte[2]=A 协议代码
                        val32 = result["value"] & 0xFFFFFFFF
                        c3_proto = (val32 >> 24) & 0xFF
                        a_proto = (val32 >> 8) & 0xFF
                        await self.state.set_hw_protocol_codes_c3a(c3_proto, a_proto)
                        _LOGGER.info("PIID18 hw_protocol_codes: C3=%d A=%d (raw=0x%08X)",
                                     self.state._hw_protocol_c3, self.state._hw_protocol_a, val32)
                    elif piid == 21:
                        await self.state.update_protocol_extend(result["value"])
                        self._sse_emit("protocol", {"switches": self.state.protocol_switches,
                                                    "protocol_extend": result["value"]})
            except Exception as e:
                fail_count += 1
                _LOGGER.debug("Failed to read PIID %d: %s", piid, e)
        if fail_count >= len(READABLE_SETTINGS_PIIDS):
            _LOGGER.warning("All %d PIID reads failed, BLE channel may be broken", fail_count)
        await self.state.update_settings(settings)
        await self.state.update_pdo_caps(pdo_caps)
        _invalidate()
        self._publish_settings(retain=True)
        # Detect port control changes (PIID 16) — firmware may close ports via countdown
        self._emit_port_control_changes()

    def _emit_port_state(self, piid: int, port_info: dict = None):
        """Unified port state emission: build data + publish MQTT + emit SSE.

        Args:
            piid: Port ID (1-4)
            port_info: Optional pre-built port data dict (e.g. from BLE decode).
                       If None, reads from state based on PIID 16 enabled flag.
        """
        port_ctl = self.state.settings.get("16", 0x0F)
        is_enabled = bool(port_ctl & (1 << (piid - 1)))

        if port_info is not None:
            # Caller provided data (e.g. BLE push decoded data)
            data = dict(port_info)
            data["enabled"] = is_enabled  # PIID 16 port control; port_info.active tells device presence
        elif is_enabled:
            # Port enabled — use current state
            ps = self.state.ports.get(piid)
            data = ps.to_dict() if ps else dict(PORT_DEFAULT)
            data["enabled"] = True
        else:
            # Port disabled — use zeros
            data = dict(PORT_DEFAULT)
            data["enabled"] = False

        self._publish_port(PORT_NAMES[piid], data, retain=True)
        self._sse_emit("port_update", {"port_id": piid, "port": PORT_NAMES[piid], "data": data})

    def _emit_port_control_changes(self):
        """Check if PIID 16 (port control) changed and emit SSE events for affected ports."""
        new_ctl = self.state.settings.get("16", 0x0F)
        old_ctl = getattr(self, '_last_port_ctl', None)
        self._last_port_ctl = new_ctl
        if old_ctl is None or old_ctl == new_ctl:
            return
        for piid in range(1, 5):
            bit = 1 << (piid - 1)
            was_on = bool(old_ctl & bit)
            now_on = bool(new_ctl & bit)
            if was_on != now_on:
                self._emit_port_state(piid)
                _LOGGER.info("Port %s %s (PIID16 changed: 0x%02X→0x%02X)",
                             PORT_NAMES[piid], "enabled" if now_on else "disabled",
                             old_ctl, new_ctl)

    async def _read_initial_settings(self):
        await self._fetch_settings(update_existing=False)
        for piid, pname in PORT_NAMES.items():
            self._publish_port(pname, PORT_DEFAULT, retain=True)

    async def _refresh_settings(self):
        await self._fetch_settings(update_existing=True)

    async def _process_commands(self):
        while True:
            try:
                cmd_type, cmd_data, cmd_future = self.cmd_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if cmd_type == "set":
                    await self._handle_set_command(cmd_data, cmd_future)
                elif cmd_type == "port":
                    await self._handle_port_command(cmd_data, cmd_future)
                elif cmd_type == "verify_port":
                    await self._handle_verify_port(cmd_data, cmd_future)

            except Exception as e:
                _LOGGER.error("Command error: %s", e)
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_set_command(self, cmd_data, cmd_future):
        piid, value = cmd_data
        try:
            await self.ctrl.send_miot_command(2, piid, value=value)
            await self.state.update_settings({str(piid): value})
            # 同步协议扩展缓存，防止后续 toggle 读到过期值
            if piid == 21:
                await self.state.update_protocol_extend(value)
                self._sse_emit("protocol", {"switches": self.state.protocol_switches,
                                            "protocol_extend": value})
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True})
        except Exception as e:
            _LOGGER.error("Set command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_port_command(self, cmd_data, cmd_future):
        port, action = cmd_data
        try:
            cur = await self.ctrl.send_miot_command(2, 16)
            cur_val = cur.get("value", 0) if cur else 0
            if cur is None:
                _LOGGER.warning('Failed to read port state, using 0')
            if port == "all":
                new_val = 0x0F if action == "on" else 0x00
            else:
                bit = PORT_BITS[port]
                new_val = cur_val | (1 << bit) if action == "on" else cur_val & ~(1 << bit)
            if new_val != cur_val:
                await self.ctrl.send_miot_command(2, 16, value=new_val)
                await self.state.update_settings({"16": new_val})
                # Emit port state for all changed ports (SSE + MQTT)
                if port == "all":
                    for piid in range(1, 5):
                        if not bool(new_val & (1 << (piid - 1))):
                            if piid in self._active_sessions:
                                self._close_session(piid, time.time())
                            await self.state.update_port(piid, PORT_DEFAULT)
                        self._emit_port_state(piid)
                else:
                    piid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(port)
                    if piid:
                        if action == "off":
                            if piid in self._active_sessions:
                                self._close_session(piid, time.time())
                            await self.state.update_port(piid, PORT_DEFAULT)
                        self._emit_port_state(piid)
                _invalidate()
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": new_val})
        except Exception as e:
            _LOGGER.error("Port command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_verify_port(self, cmd_data, cmd_future):
        """Actively GET a port that has been idle too long, to detect a missed
        unplug (push frame lost while we were busy). Updates ps from the live
        reading so a stale V/I doesn't persist indefinitely."""
        piid = cmd_data if isinstance(cmd_data, int) else (cmd_data[0] if isinstance(cmd_data, (list, tuple)) else None)
        self._pending_verify.discard(piid)
        if not piid or piid not in range(1, 5) or not self.ctrl:
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": "invalid piid"})
            return
        try:
            result = await self.ctrl.send_miot_command(2, piid)
            self._last_verify_time[piid] = time.time()
            if not result:
                # No response — connection may be stale; leave as-is.
                _LOGGER.debug("verify_port piid=%d: no response", piid)
                if cmd_future and not cmd_future.done():
                    cmd_future.set_result({"ok": False, "error": "no response"})
                return
            raw = result.get("raw")
            value = result.get("value")
            # Diagnostic: log raw GET value so we can verify the byte-layout
            # assumption below against actual device frames.
            _LOGGER.info("verify_port piid=%d raw_value=0x%08X raw_len=%d",
                         piid, value if isinstance(value, int) else -1,
                         len(raw) if raw else 0)
            # GET result value carries the port raw status bytes
            # [status, code, current_raw, voltage_raw] in low 4 bytes.
            if isinstance(value, int) and value >= 0:
                raw_bytes = value.to_bytes(4, 'little')
                cur_raw = raw_bytes[2]
                vol_raw = raw_bytes[3]
                voltage = vol_raw / 10.0
                current = cur_raw / 10.0
                old = self.state.ports.get(piid)
                # Only correct if the live reading differs meaningfully from
                # what we hold — prevents feedback loops on noise.
                if old is None or (voltage, current) != (old.voltage, old.current):
                    if voltage <= 0 and current <= 0:
                        # Device unplugged (V=0, I=0): clear the stale port state.
                        if piid in self._active_sessions:
                            self._close_session(piid, time.time())
                        port_info = dict(PORT_DEFAULT)
                        await self.state.update_port(piid, PORT_DEFAULT)
                        _LOGGER.info("verify_port: piid=%d unplugged (V=0,I=0), cleared stale state", piid)
                    else:
                        port_info = {
                            "voltage": round(voltage, 1),
                            "current": round(current, 1),
                            "power": round(voltage * current, 1),
                            "active": True,
                            "protocol": old.protocol if old else "idle",
                        }
                        await self.state.update_port(piid, port_info)
                    _invalidate()
                    self._emit_port_state(piid, port_info)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": value})
        except Exception as e:
            _LOGGER.warning("verify_port piid=%d error: %s", piid, e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_inline_data(self, data):
        if not self.ctrl:
            return
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
        await self._try_process_inline_frame(data)

    async def _try_process_inline_frame(self, raw_data):
        """Try to decrypt and process a raw BLE frame as inline port data.
        
        Shared between _handle_inline_data and _handle_multiframe.
        Silently returns if data doesn't match inline format.
        """
        if not self.ctrl:
            return
        _LOGGER.debug("inline_frame: raw=%s len=%d", raw_data.hex() if raw_data else "null", len(raw_data) if raw_data else 0)
        encrypted_payload = raw_data[4:]
        pt = self.ctrl.decrypt(encrypted_payload)
        if pt:
            _LOGGER.debug("inline_frame: decrypted=%s len=%d", pt.hex(), len(pt))
        if not pt or len(pt) < 8:
            if not pt:
                # Diagnostic: log frame header + it so we can tell whether the
                # failure is a key mismatch (device re-keyed) or frame misalignment.
                it = raw_data[:2].hex() if raw_data and len(raw_data) >= 2 else "??"
                kind = "single" if raw_data and len(raw_data) >= 3 and raw_data[2] == 0x02 else ("multi" if raw_data and len(raw_data) >= 3 and raw_data[2] == 0x00 else "other")
                _LOGGER.debug("inline_frame: decrypt failed (kind=%s it=0x%s)", kind, it)
            else:
                _LOGGER.debug("inline_frame: too short (%d < 8)", len(pt))
            self._decrypt_failures += 1
            if self._decrypt_failures >= 3:
                _LOGGER.warning("Decrypt failed %d times consecutively, session stale, triggering reconnect", self._decrypt_failures)
                raise ConnectionError("Session stale due to consecutive decrypt failures")
            return
        self._decrypt_failures = 0
        b4 = pt[4]
        piid = pt[7] if len(pt) > 7 else -1

        # 优先使用 PIID 17 的硬件协议代码 (c1_c2_protocol Spec 属性)
        # 与米家 parseC1C2ProtocolInfo 一致: byte[0]=C1, byte[2]=C2
        hw_protocol = await self.state.get_hw_protocol(piid)

        if b4 == 0x04 and piid in PORT_NAMES:
            pdo_data = None
            if piid in (1, 2):
                pdo_data = self.state.pdo_caps.get("c1c2", {}).get(PORT_NAMES[piid])
            elif piid in (3, 4):
                pdo_data = self.state.pdo_caps.get("c3a", {}).get(PORT_NAMES[piid])
            port_info = decode_port(piid, pt, pdo_data,
                                    protocol_switches=self.state.protocol_switches,
                                    hw_protocol=hw_protocol)
            if port_info:
                _LOGGER.info("Port %s update: %s", PORT_NAMES[piid], port_info)
            else:
                _LOGGER.debug("Port %s: decode_port returned None (pt=%s)", PORT_NAMES[piid], pt.hex())
            if port_info:
                # Protocol debounce: only update protocol after N consecutive same readings
                new_proto = port_info.get("protocol", "")
                buf = self._proto_buf[piid]
                buf.append(new_proto)
                if len(buf) > self._PROTO_DEBOUNCE_N:
                    buf.pop(0)
                old = self.state.ports.get(piid)
                if old and len(buf) >= self._PROTO_DEBOUNCE_N:
                    if len(set(buf)) == 1:
                        # All N readings are the same — stable, use new protocol
                        pass
                    else:
                        # Not stable yet — keep old protocol
                        port_info["protocol"] = old.protocol
                # Port idle → clear protocol immediately (don't let debounce block it)
                if not port_info.get("active", True):
                    port_info["protocol"] = "idle"
                    self._proto_buf[piid].clear()
                await self.state.update_port(piid, port_info)
                if old is None or old.to_dict() != port_info:
                    _invalidate()
                    self._emit_port_state(piid, port_info)

                # ── Data processing: runs on EVERY push, not gated by change detection ──
                voltage = port_info.get("voltage", 0)
                current = port_info.get("current", 0)
                timestamp = time.time()
                es = self._energy_states[piid]
                det = self._charge_detectors[piid]

                # Accumulate energy (trapezoidal integration needs continuous timestamps)
                self._energy_integrator.update(es, voltage, current, timestamp)
                det.update(voltage * current, timestamp)

                # Check gradual power decline on every push (not just low-current)
                if es.is_charging and det.should_end_session(es, timestamp):
                    self._low_current_count[piid] = 0
                    sid = self._close_session(piid, timestamp, voltage, current)
                    if sid and sid > 0:
                        _LOGGER.info("Det ended session %d (port %d, %.1fWh)",
                                     sid, piid, es.session_wh)
                    return  # Session ended by detector, skip normal session management

                # Session management
                active = port_info.get("active", False)
                start_threshold = 0.1
                if es.last_end_time and (timestamp - es.last_end_time) < 60:
                    start_threshold = 0.3

                if active and current > start_threshold and not es.is_charging:
                    # Start new session（内存会话生命周期始终完整：即使记录关闭也
                    # 保持实时显示；仅 DB 写入受 record_sessions 控制）
                    self._low_current_count[piid] = 0
                    es.is_charging = True
                    es.session_wh = 0
                    es.session_start = timestamp
                    es.max_power = voltage * current
                    es.max_current = current
                    if self._history:
                        loop = asyncio.get_running_loop()
                        protocol = port_info.get("protocol", "")
                        if self.record_sessions:
                            task = loop.run_in_executor(None, self._history.start_session, piid, protocol)
                            def _on_session_start(t, p=piid):
                                if t.exception():
                                    _LOGGER.error("Start session failed for port %d: %s", p, t.exception())
                                    return
                                new_sid = t.result()
                                if not new_sid:
                                    _LOGGER.error("Start session failed for port %d: DB returned no sid", p)
                                    return
                                with self._sess_lock:
                                    es2 = self._energy_states.get(p)
                                    if es2 is None or not es2.is_charging or self._active_sessions.get(p) is not None:
                                        # 回调前会话已结束/被新会话取代：闭合刚建的 DB 行
                                        self._close_resumed_orphan(p, new_sid)
                                        return
                                    self._active_sessions[p] = new_sid
                            task.add_done_callback(_on_session_start)
                        else:
                            # 记录关闭：不写库，使用内存伪造负 sid 保持会话
                            # 实时显示/充电完成事件正常（负值不与真实会话冲突）
                            self._fake_sid_counter -= 1
                            with self._sess_lock:
                                self._active_sessions[piid] = self._fake_sid_counter

                elif not active and es.is_charging:
                    # Port closed — end session immediately (no debounce needed)
                    self._low_current_count[piid] = 0
                    self._close_session(piid, timestamp, voltage, current)

                elif current <= 0.1 and es.is_charging:
                    # Current dropped — debounce before ending
                    self._low_current_count[piid] += 1
                    # Also check ChargeEndDetector for gradual power decline
                    if self._low_current_count[piid] >= self._LOW_CURRENT_N or det.should_end_session(es, timestamp):
                        self._low_current_count[piid] = 0
                        sid = self._close_session(piid, timestamp, voltage, current)
                        if sid and sid > 0:
                            _LOGGER.info("LowCurrent ended session %d (port %d, %.1fWh)",
                                         sid, piid, es.session_wh)
                # Catch missed end_session: port turns off but session not tracked
                elif current <= 0.1 and not es.is_charging and piid in self._active_sessions:
                    sid = self._close_session(piid, timestamp)
                    if sid and sid > 0:
                        _LOGGER.warning("Closing stale session %d on port %d", sid, piid)

                # Record charge points (every push during active session)
                if self._history and es.is_charging and piid in self._active_sessions:
                    self._record_charge_point(
                        piid, voltage, current, port_info.get("protocol", ""))

                # Record to history (existing)
                if self._history and port_info.get("active", False):
                    loop = asyncio.get_running_loop()
                    task = loop.run_in_executor(None, self._history.record_port_data, piid, port_info)
                    task.add_done_callback(
                        lambda t: _LOGGER.error("History write failed: %s", t.exception()) if t.exception() else None)

    async def _handle_multiframe(self, data):
        """Handle multi-frame BLE data. ACK protocol + attempt inline processing.
        
        Multi-frame is used for settings batch pushes and large responses.
        The ACK (RCV_RDY + RCV_OK) is required to keep the BLE channel in sync.
        Individual frames are also attempted as inline data for robustness.
        """
        if not self.ctrl:
            return
        frame_count = data[4] + 0x100 * data[5]
        if frame_count > 1000:
            _LOGGER.warning("Multiframe count too large: %d, consuming all frames", frame_count)
            await self.ctrl.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
            for i in range(frame_count):
                try:
                    frame = await asyncio.wait_for(
                        self.ctrl.wait_notify("cmd_recv", timeout=3.0), timeout=5.0)
                    if frame:
                        await self._try_process_inline_frame(frame)
                except ConnectionError:
                    # Session is stale (consecutive decrypt failures) — let it
                    # propagate so the caller reconnects instead of swallowing it.
                    raise
                except (asyncio.TimeoutError, Exception) as e:
                    _LOGGER.warning("Multiframe drain stopped at frame %d/%d: %s", i+1, frame_count, e)
                    break
            await self.ctrl.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
            return
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
        received_count = 0
        for _ in range(frame_count):
            frame = await self.ctrl.wait_notify("cmd_recv", timeout=3.0)
            if frame:
                received_count += 1
                await self._try_process_inline_frame(frame)
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
        if received_count != frame_count:
            _LOGGER.debug("Multiframe: received %d/%d frames", received_count, frame_count)

    def _publish_status(self, payload, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(self.config.topic_status, payload, retain=retain)
        self._sse_emit("status", payload)

    def _publish_settings(self, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(self.config.topic_settings, self.state.settings, retain=retain)
        self._sse_emit("settings", {"settings": self.state.settings})

    def _publish_port(self, port_name, data, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(f"{self.config.topic_port}/{port_name}", data, retain=retain)

    async def send_command(self, cmd_type, cmd_data, timeout=None):
        if not self.ctrl or not self.state.authenticated:
            return {"ok": False, "error": "not connected"}
        timeout = timeout or self.config.server.command_timeout
        future = asyncio.get_running_loop().create_future()
        await self.cmd_queue.put((cmd_type, cmd_data, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "command timeout"}
