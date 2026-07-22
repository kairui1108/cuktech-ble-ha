"""CUKTECH BLE Server - BLE connection manager with auto-reconnect."""
import asyncio
import logging
import sys
import os
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
    def __init__(self, mac, token, state, config):
        self.mac = mac
        self.token = bytes.fromhex(token)
        self.state = state
        self.config = config
        self.ctrl = None
        self.cmd_queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._port_timer_task = None
        self._mqtt_publish = None
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._auth_fail_count = 0
        self._base_reconnect_delay = config.server.reconnect_base_delay
        self._max_reconnect_delay = config.server.reconnect_max_delay
        self._history = None
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
        self._LOW_CURRENT_N = 10  # consecutive readings below threshold to end session

    def set_mqtt_publisher(self, publisher):
        self._mqtt_publish = publisher

    def set_history(self, history):
        self._history = history

    @property
    def is_running(self) -> bool:
        """是否正在运行 (不处于停止状态)。"""
        return not self._stop_event.is_set()

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
        """Gracefully close all active charge sessions on shutdown."""
        now = time.time()
        for port, es in self._energy_states.items():
            if es.is_charging and port in self._active_sessions and self._history:
                sid = self._active_sessions.pop(port)
                duration = int(now - (es.session_start or now))
                det = self._charge_detectors[port]
                det.on_session_end(now)
                es.is_charging = False
                es.last_end_time = now
                if es.session_wh >= 0.05:
                    ps = self.state.ports.get(port)
                    if ps:
                        self._publish_charge_event(
                            port, sid, es, now,
                            ps.voltage, ps.current, duration)
                    _LOGGER.info("Closing session %d (port %d, %.1fWh, %ds)", sid, port, es.session_wh, duration)
                    self._history.end_session(sid, es.session_wh, es.max_power, 0, 0, duration)

    def _close_session(self, piid, timestamp, voltage=0, current=0):
        """Close a charge session: cleanup state and write to DB.
        Synchronous — no await, safe to call from any non-async context.
        Returns sid if a session was closed, None otherwise.
        """
        det = self._charge_detectors[piid]
        es = self._energy_states[piid]
        sid = self._active_sessions.pop(piid, None)
        if not sid:
            return None
        det.on_session_end(timestamp)
        es.is_charging = False
        es.last_end_time = timestamp
        if sid and self._history:
            duration = int(timestamp - (es.session_start or timestamp))
            if es.session_wh < 0.05:
                task = asyncio.get_running_loop().run_in_executor(
                    None, self._history.delete_session, sid)
            else:
                # Publish charge completion event via MQTT
                self._publish_charge_event(piid, sid, es, timestamp,
                                           voltage, current, duration)
                task = asyncio.get_running_loop().run_in_executor(
                    None, self._history.end_session, sid,
                    round(es.session_wh, 4), round(es.max_power, 2),
                    round(voltage, 2), round(current, 2), duration)
            task.add_done_callback(
                lambda t, _sid=sid: _LOGGER.error("Close session %d failed: %s", _sid, t.exception()) if t.exception() else None)
        return sid

    def _publish_charge_event(self, piid, sid, es, timestamp, voltage, current, duration):
        """Publish charge completion event via MQTT."""
        if not self._mqtt_publish:
            return
        try:
            ps = self.state.ports.get(piid)
            payload = {
                "event": "charge_end",
                "port": PORT_NAMES.get(piid, str(piid)),
                "port_id": piid,
                "session_id": sid,
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
        """Calculate exponential backoff delay."""
        delay = min(
            self._base_reconnect_delay * (2 ** min(self._reconnect_attempts, 10)),
            self._max_reconnect_delay
        )
        return delay

    async def start(self):
        self._stop_event.clear()
        self._reconnect_attempts = 0
        self._decrypt_failures = 0
        self._auth_fail_count = 0
        first_run = True
        last_error = None
        while not self._stop_event.is_set():
            try:
                await self._connect_and_run()
                self._reconnect_attempts = 0
                self._decrypt_failures = 0
                self._auth_fail_count = 0
                first_run = False
                last_error = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                if 'POWERED_OFF' in err_str or 'No powered Bluetooth' in err_str:
                    _LOGGER.warning("Bluetooth is powered off, will retry in 60s...")
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
                elif last_error:
                    await self._force_disconnect_bluetooth()
                    delay = self._get_reconnect_delay()
                else:
                    delay = self._get_reconnect_delay()
                self._reconnect_attempts += 1
                if 'POWERED_OFF' not in str(last_error or '') and 'No powered Bluetooth' not in str(last_error or ''):
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
        from bleak import BleakScanner
        try:
            found = await BleakScanner.find_device_by_address(
                self.mac, timeout=self.config.ble.scan_timeout)
        except Exception as e:
            _LOGGER.error("BLE scan failed: %s", e)
            raise ConnectionError(f"BLE scan failed: {e}")
        if not found:
            _LOGGER.error("Charger not found with MAC: %s", self.mac)
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
        await self.state.set_connection(True, True)
        _invalidate()
        _LOGGER.info("Authenticated!")
        self._publish_status({
            "connected": True,
            "authenticated": True,
            "device_model": self.ctrl.device_model,
            "firmware_version": self.ctrl.firmware_version,
        }, retain=True)

        await self._read_initial_settings()
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

    async def _force_disconnect_bluetooth(self):
        """使用 bluetoothctl 强制断开蓝牙连接并重置适配器。

        仅在 Linux + bluetoothctl 可用时执行；其它平台由 bleak 层处理断连，
        适配器电源循环属于 Linux 特有的 BlueZ 恢复手段，跳过不影响功能。
        """
        if not _has_bluetoothctl():
            _LOGGER.info("bluetoothctl not available, skipping adapter power cycle")
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", self.mac,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            # 等待 BLE Link Layer disconnect 完成
            await asyncio.sleep(3)
            _LOGGER.info("BLE disconnect confirmed")
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
                except asyncio.TimeoutError:
                    now = time.time()
                    if now - last_refresh > self.config.server.settings_refresh_interval:
                        await self._refresh_settings()
                        now = time.time()
                        last_refresh = now
                        last_notify = now
                    if now - last_keepalive > 10:
                        if self.ctrl and self.ctrl.client and self.ctrl.client.is_connected:
                            try:
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
                        if not client or not client.is_connected:
                            _LOGGER.warning("BLE connection lost, triggering reconnect")
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
        that are NOT receiving BLE pushes (stable V/I)."""
        while not self._stop_event.is_set():
            await asyncio.sleep(1)
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
                            if sid:
                                _LOGGER.info("Timer ended session %d (port %d, %.1fWh)",
                                             sid, piid, es.session_wh)
                        else:
                            sid = self._active_sessions.get(piid)
                            if sid:
                                task = loop.run_in_executor(
                                    None, self._history.record_charge_point,
                                    sid, ps.voltage, ps.current,
                                    round(ps.voltage * ps.current, 1),
                                    ps.protocol or "")
                                task.add_done_callback(
                                    lambda t: _LOGGER.error("Timer record_charge_point failed: %s", t.exception()) if t.exception() else None)
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
                    elif piid == 18:
                        pdo_caps["c3a"] = decode_pdo_caps(result["value"], "c3", "a")
                    elif piid == 21:
                        await self.state.update_protocol_extend(result["value"])
            except Exception as e:
                fail_count += 1
                _LOGGER.debug("Failed to read PIID %d: %s", piid, e)
        if fail_count >= len(READABLE_SETTINGS_PIIDS):
            _LOGGER.warning("All %d PIID reads failed, BLE channel may be broken", fail_count)
        await self.state.update_settings(settings)
        await self.state.update_pdo_caps(pdo_caps)
        _invalidate()
        self._publish_settings(retain=True)

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
                # 端口关闭时清零端口数据
                if action == "off" and port != "all":
                    piid = {"c1": 1, "c2": 2, "c3": 3, "a": 4}.get(port)
                    if piid:
                        await self.state.update_port(piid, PORT_DEFAULT)
                        _invalidate()
                        self._publish_port(PORT_NAMES[piid], PORT_DEFAULT, retain=True)
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": new_val})
        except Exception as e:
            _LOGGER.error("Port command error: %s", e)
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
        encrypted_payload = raw_data[4:]
        pt = self.ctrl.decrypt(encrypted_payload)
        if not pt or len(pt) < 8:
            self._decrypt_failures += 1
            if self._decrypt_failures >= 10:
                _LOGGER.warning("Decrypt failed %d times consecutively, session stale, triggering reconnect", self._decrypt_failures)
                raise ConnectionError("Session stale due to consecutive decrypt failures")
            return
        self._decrypt_failures = 0
        b4 = pt[4]
        piid = pt[7] if len(pt) > 7 else -1
        if b4 == 0x04 and piid in PORT_NAMES:
            pdo_data = None
            if piid in (1, 2):
                pdo_data = self.state.pdo_caps.get("c1c2", {}).get(PORT_NAMES[piid])
            elif piid in (3, 4):
                pdo_data = self.state.pdo_caps.get("c3a", {}).get(PORT_NAMES[piid])
            port_info = decode_port(piid, pt, pdo_data,
                                    protocol_switches=self.state.protocol_switches)
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
                    port_info["protocol"] = ""
                    self._proto_buf[piid].clear()
                await self.state.update_port(piid, port_info)
                if old is None or old.to_dict() != port_info:
                    _invalidate()
                    self._publish_port(PORT_NAMES[piid], port_info, retain=True)

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
                    if sid:
                        _LOGGER.info("Det ended session %d (port %d, %.1fWh)",
                                     sid, piid, es.session_wh)
                    return  # Session ended by detector, skip normal session management

                # Session management
                active = port_info.get("active", False)
                start_threshold = 0.1
                if es.last_end_time and (timestamp - es.last_end_time) < 60:
                    start_threshold = 0.3

                if active and current > start_threshold and not es.is_charging:
                    # Start new session
                    self._low_current_count[piid] = 0
                    es.is_charging = True
                    es.session_wh = 0
                    es.session_start = timestamp
                    es.max_power = voltage * current
                    es.max_current = current
                    if self._history:
                        loop = asyncio.get_running_loop()
                        protocol = port_info.get("protocol", "")
                        task = loop.run_in_executor(None, self._history.start_session, piid, protocol)
                        def _on_session_start(t, p=piid):
                            if not t.exception():
                                self._active_sessions[p] = t.result()
                        task.add_done_callback(_on_session_start)

                elif not active and es.is_charging and piid in self._active_sessions:
                    # Port closed — end session immediately (no debounce needed)
                    self._low_current_count[piid] = 0
                    self._close_session(piid, timestamp, voltage, current)

                elif current <= 0.1 and es.is_charging:
                    # Current dropped — debounce before ending
                    self._low_current_count[piid] += 1
                    # Also check ChargeEndDetector for gradual power decline
                    if self._low_current_count[piid] >= self._LOW_CURRENT_N or det.should_end_session(es, timestamp):
                        self._low_current_count[piid] = 0
                        self._close_session(piid, timestamp, voltage, current)
                # Catch missed end_session: port turns off but session not tracked
                elif current <= 0.1 and not es.is_charging and piid in self._active_sessions:
                    sid = self._close_session(piid, timestamp)
                    if sid:
                        _LOGGER.warning("Closing stale session %d on port %d", sid, piid)

                # Record charge points (every push during active session)
                if self._history and es.is_charging and piid in self._active_sessions:
                    sid = self._active_sessions.get(piid)
                    if sid:
                        loop = asyncio.get_running_loop()
                        proto = port_info.get("protocol", "")
                        task = loop.run_in_executor(
                            None, self._history.record_charge_point,
                            sid, voltage, current, voltage * current, proto)
                        task.add_done_callback(
                            lambda t: _LOGGER.error("Record point failed: %s", t.exception()) if t.exception() else None)

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

    def _publish_settings(self, retain=False):
        if self._mqtt_publish:
            self._mqtt_publish(self.config.topic_settings, self.state.settings, retain=retain)

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
