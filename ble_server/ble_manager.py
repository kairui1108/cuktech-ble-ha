"""CUKTECH BLE Server - BLE connection manager with auto-reconnect."""
import asyncio
import logging
import sys
import os
import time

try:
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION

from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT, decode_port, decode_pdo_caps

_LOGGER = logging.getLogger("cuktech_ble")

_status_cache_invalidator = None


def set_status_cache_invalidator(invalidator):
    global _status_cache_invalidator
    _status_cache_invalidator = invalidator


def _invalidate():
    if _status_cache_invalidator:
        _status_cache_invalidator()


class BLEManager:
    def __init__(self, mac, token, state, config):
        self.mac = mac
        self.token = bytes.fromhex(token)
        self.state = state
        self.config = config
        self.ctrl = None
        self.cmd_queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._mqtt_publish = None
        self._reconnect_attempts = 0
        self._base_reconnect_delay = config.server.reconnect_base_delay
        self._max_reconnect_delay = config.server.reconnect_max_delay
        self._history = None

    def set_mqtt_publisher(self, publisher):
        self._mqtt_publish = publisher

    def set_history(self, history):
        self._history = history

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
        first_run = True
        last_error = None
        while not self._stop_event.is_set():
            try:
                await self._connect_and_run()
                self._reconnect_attempts = 0
                first_run = False
                last_error = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                last_error = e
                _LOGGER.error("BLE loop error: %s", e)
            finally:
                await self._disconnect()
            if not self._stop_event.is_set():
                await self._force_disconnect_bluetooth()
                delay = self._get_reconnect_delay()
                self._reconnect_attempts += 1
                _LOGGER.info("Reconnecting in %.0fs (attempt %d)...", delay, self._reconnect_attempts)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

    async def stop(self):
        self._stop_event.set()
        await self._disconnect()
        await self._force_disconnect_bluetooth()

    async def _connect(self):
        _LOGGER.info("Scanning for charger...")
        from bleak import BleakScanner
        try:
            found = await BleakScanner.find_device_by_address(self.mac, timeout=10)
        except Exception as e:
            _LOGGER.error("BLE scan failed: %s", e)
            raise ConnectionError(f"BLE scan failed: {e}")
        if not found:
            _LOGGER.error("Charger not found with MAC: %s", self.mac)
            raise ConnectionError("Charger not found")

        self.ctrl = CuktechBLEController(self.mac, self.token)
        await self.ctrl.connect()

        _LOGGER.info("Connected, waiting for device to settle...")
        await asyncio.sleep(1)

        await self.ctrl.read_device_info()
        _LOGGER.info("Connected, authenticating...")

        if not await self.ctrl.authenticate():
            _LOGGER.warning("Auth failed, waiting 5s for device state to clear...")
            try:
                await self.ctrl.client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(5)
            raise ConnectionError("Auth failed")

        await self.state.set_connection(True, True)
        _invalidate()
        _LOGGER.info("Authenticated!")
        self._publish_status({"connected": True, "authenticated": True}, retain=True)

        await self._read_initial_settings()
        await asyncio.sleep(2)

    async def _disconnect(self):
        if self.ctrl:
            client = self.ctrl.client if self.ctrl else None
            was_connected = client and client.is_connected if client else False
            try:
                if client and client.is_connected:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=3.0)
                    except Exception:
                        pass
            except Exception:
                pass
            self.ctrl = None
            if was_connected and not self._stop_event.is_set():
                _LOGGER.error("BLE device disconnected unexpectedly")
        await self.state.set_connection(False, False)
        _invalidate()
        self._publish_status({"connected": False}, retain=True)

    async def _force_disconnect_bluetooth(self):
        """使用 bluetoothctl 强制断开蓝牙连接并重置适配器"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", self.mac,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
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
            await asyncio.sleep(3)
        except Exception as e:
            _LOGGER.warning("bluetoothctl power cycle failed: %s", e)

    async def _connect_and_run(self):
        await self._connect()
        last_refresh = time.time()
        last_notify = time.time()
        last_keepalive = time.time()

        while not self._stop_event.is_set():
            await self._process_commands()

            try:
                data = await asyncio.wait_for(
                    self.ctrl.wait_notify("cmd_recv"), timeout=2.0)
                last_notify = time.time()
            except asyncio.TimeoutError:
                now = time.time()
                if now - last_refresh > self.config.server.settings_refresh_interval:
                    await self._refresh_settings()
                    last_refresh = now
                if now - last_keepalive > 10:
                    try:
                        await self.ctrl.client.read_gatt_char(CHAR_FW_VERSION)
                        last_keepalive = now
                    except Exception:
                        client = self.ctrl.client if self.ctrl else None
                        if not client or not client.is_connected:
                            _LOGGER.warning("BLE keepalive: connection lost, triggering reconnect")
                            raise ConnectionError("BLE disconnected via keepalive")
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

    async def _fetch_settings(self, update_existing=False):
        settings = dict(self.state.settings) if update_existing else {}
        pdo_caps = {}
        for piid in [5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20]:
            try:
                result = await self.ctrl.send_miot_command(2, piid)
                if result and "value" in result:
                    settings[str(piid)] = result["value"]
                    if piid == 17:
                        pdo_caps["c1c2"] = decode_pdo_caps(result["value"], "c1", "c2")
                    elif piid == 18:
                        pdo_caps["c3a"] = decode_pdo_caps(result["value"], "c3", "a")
            except Exception as e:
                _LOGGER.debug("Failed to read PIID %d: %s", piid, e)
            await asyncio.sleep(0.1)
        await self.state.update_settings(settings)
        await self.state.update_pdo_caps(pdo_caps)
        _invalidate()
        self._publish_settings(retain=True)

    async def _read_initial_settings(self):
        await self._fetch_settings(update_existing=False)
        for piid, pname in PORT_NAMES.items():
            self._publish_port(pname, PORT_DEFAULT)

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
            cur_val = cur.get("value", 0x0F) if cur else 0x0F
            if port == "all":
                new_val = 0x0F if action == "on" else 0x00
            else:
                bit = PORT_BITS[port]
                new_val = cur_val | (1 << bit) if action == "on" else cur_val & ~(1 << bit)
            if new_val != cur_val:
                await self.ctrl.send_miot_command(2, 16, value=new_val)
            await self.state.update_settings({"16": new_val})
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": new_val})
        except Exception as e:
            _LOGGER.error("Port command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_inline_data(self, data):
        encrypted_payload = data[4:]
        await self.ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
        pt = self.ctrl.decrypt(encrypted_payload)
        if not pt or len(pt) < 8:
            return
        b4 = pt[4]
        piid = pt[7] if len(pt) > 7 else -1
        if b4 == 0x04 and piid in PORT_NAMES:
            pdo_data = None
            if piid in (1, 2):
                pdo_data = self.state.pdo_caps.get("c1c2", {}).get(PORT_NAMES[piid])
            elif piid in (3, 4):
                pdo_data = self.state.pdo_caps.get("c3a", {}).get(PORT_NAMES[piid])
            port_info = decode_port(piid, pt, pdo_data)
            if port_info:
                old = self.state.ports.get(piid)
                await self.state.update_port(piid, port_info)
                if old is None or old.to_dict() != port_info:
                    _invalidate()
                    self._publish_port(PORT_NAMES[piid], port_info)
                    if self._history and port_info.get("active", False):
                        loop = asyncio.get_running_loop()
                        task = loop.run_in_executor(None, self._history.record_port_data, piid, port_info)
                        task.add_done_callback(
                            lambda t: _LOGGER.error("History write failed: %s", t.exception()) if t.exception() else None)

    async def _handle_multiframe(self, data):
        """Handle multi-frame BLE data. Data is ACKed but not processed further.

        Multi-frame is used for settings batch pushes and large responses.
        The ACK (RCV_RDY + RCV_OK) is required to keep the BLE channel in sync.
        Actual data processing happens via inline notifications.
        """
        frame_count = data[4] + 0x100 * data[5]
        if frame_count > 1000:
            _LOGGER.warning("Multiframe count too large: %d, consuming all frames", frame_count)
            # Still send ACK to keep protocol in sync
            await self.ctrl.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
            # Consume all remaining frames in a tight loop
            for i in range(frame_count):
                try:
                    await asyncio.wait_for(
                        self.ctrl.wait_notify("cmd_recv", timeout=3.0), timeout=5.0)
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

    def _publish_port(self, port_name, data):
        if self._mqtt_publish:
            self._mqtt_publish(f"{self.config.topic_port}/{port_name}", data)

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
