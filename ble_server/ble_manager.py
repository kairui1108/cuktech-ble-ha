"""CUKTECH BLE Server - BLE connection manager with auto-reconnect."""
import asyncio
import logging
import sys
import os
import time
import struct

try:
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS
    from state_protocol_v2 import get_mijia_protocol_name
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
    from cuktech_ble.controller import CuktechBLEController, CHAR_CMD_RECV, CHAR_FW_VERSION, AuthConnectionError
    from cuktech_ble.protocol import READABLE_SETTINGS_PIIDS

from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT, decode_port, decode_pdo_caps

# BLE Spec 探索
try:
    from cuktech_ble.protocol import CHAR_BLE_SPEC
except ImportError:
    CHAR_BLE_SPEC = "00000005-0000-1000-8000-00805f9b34fb"

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
        self._decrypt_failures = 0
        self._auth_fail_count = 0
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
                _LOGGER.error("BLE loop error: %s", e, exc_info=True)
            finally:
                await self._disconnect()
            if not self._stop_event.is_set():
                if isinstance(last_error, AuthConnectionError):
                    # auth 失败可能有两类原因:
                    # 1. 设备端 session 未清除 (需等待设备自然超时)
                    # 2. BlueZ GATT 缓存损坏 (需 power cycle 本地适配器)
                    # 因此 auth 失败也应重置本地适配器，避免陷入永久失败
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
                elif last_error:
                    await self._force_disconnect_bluetooth()
                    delay = self._get_reconnect_delay()
                else:
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

    def _find_ble_adapter(self):
        """自动检测支持 BLE 的蓝牙适配器名称（如 hci0, hci1）"""
        import glob
        hci_devs = sorted(glob.glob("/sys/class/bluetooth/hci*"))
        for hci_dir in hci_devs:
            hci_name = os.path.basename(hci_dir)
            # 跳过虚拟适配器
            if ":" in hci_name:
                continue
            if os.path.isdir(os.path.join(hci_dir, "device")):
                return hci_name
        return "hci0"

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

        # 验证 GATT 读取能力（power cycle 后 BlueZ D-Bus 可能未完全就绪）
        # 静默等待适配器完全初始化，不做激进的 GATT 检查
        await asyncio.sleep(3)

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

    def _try_parse_blespec(self, raw_data: bytes):
        """尝试解析 BLE Spec 通知数据为 32-bit 端口格式."""
        try:
            _LOGGER.debug("BLESpec raw: %s (%d bytes)", raw_data.hex()[:40], len(raw_data))
            if len(raw_data) < 4:
                return
            pt = self.ctrl.decrypt(raw_data[4:]) if self.ctrl else None
            if pt:
                _LOGGER.info("BLESpec decrypted: %s (%d bytes)", pt.hex(), len(pt))
        except Exception:
            pass

    async def ble_spec_test_write(self, payload: bytes):
        """测试 BLE Spec 写入: 向 0000001c (device_info) 发请求，监控响应."""
        if not self.ctrl or not self.ctrl.client:
            return {"ok": False, "error": "not connected"}
        client = self.ctrl.client
        results = []
        try:
            # 尝试多个可能的目标特征
            for service in client.services:
                for char in service.characteristics:
                    short = char.uuid.split('-')[0] if '-' in char.uuid else char.uuid
                    if short in ("0000001c", "00000019", "0000001a"):
                        try:
                            _LOGGER.info("BLE Spec test write to %s: %s", short, payload.hex())
                            await client.write_gatt_char(char, payload, response=False)
                            results.append(f"{short}=ok")
                        except Exception as e:
                            results.append(f"{short}={e}")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True if results else False, "results": results}

    async def ble_spec_send_protobuf(self, pb_data: bytes):
        """发送 BLE Spec protobuf 命令并获取响应."""
        if not self.ctrl or not self.ctrl.client:
            return {"ok": False, "error": "not connected"}
        try:
            _LOGGER.info("Sending spec protobuf: %s (%d bytes)", pb_data.hex(), len(pb_data))
            resp = await self.ctrl.send_spec_protobuf(pb_data)
            if resp:
                return {"ok": True, "response": resp.hex()}
            return {"ok": False, "error": "no response"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def ble_spec_protobuf_framed(self, pb_data: bytes):
        """通过 0000001c 通道 + 帧协议发送 BLE Spec protobuf."""
        if not self.ctrl or not self.ctrl.client:
            return {"ok": False, "error": "not connected"}
        client = self.ctrl.client
        try:
            encrypted = self.ctrl._encrypt(pb_data)
            
            # 找到 0000001c 特征
            target_char = None
            for service in client.services:
                for char in service.characteristics:
                    if "0000001c" in char.uuid:
                        target_char = char
                        break
            if not target_char:
                return {"ok": False, "error": "0000001c not found"}
            
            # 帧协议: 写入头部到 0000001c
            header = bytes([0x00, 0x00, 0x00, 0x00, 0x01, 0x00])
            await client.write_gatt_char(target_char, header, response=False)
            _LOGGER.info("Spec: wrote header to 0000001c")
            
            # 等待 RCV_RDY (00000101) via controller queue
            data = await self.ctrl.wait_notify("cmd_send", timeout=5.0)
            if not data or data != bytes([0x00, 0x00, 0x01, 0x01]):
                # 也尝试 cmd_recv 管道
                data = await self.ctrl.wait_notify("cmd_recv", timeout=2.0)
                if data and len(data) >= 4 and data[2] == 0x02:
                    _LOGGER.info("Spec: got inline response on cmd_recv, raw=%s", data.hex())
                    # 解密并解析
                    try:
                        encrypted_payload = data[4:]
                        pt = self.ctrl.decrypt(encrypted_payload)
                        _LOGGER.info("Spec: decrypted response: %s", pt.hex() if pt else 'None')
                    except Exception as e:
                        _LOGGER.info("Spec: decrypt failed: %s", e)
                    return {"ok": True, "via_cmd_recv": True}
                _LOGGER.warning("Spec: no RCV_RDY: %s", data.hex() if data else 'None')
                return {"ok": False, "error": f"no RCV_RDY"}
            
            # 发送数据帧到 0000001c
            frame = bytes([0x01, 0x00]) + encrypted
            await client.write_gatt_char(target_char, frame, response=False)
            _LOGGER.info("Spec: sent encrypted frame (%d bytes)", len(frame))
            
            # 等待 RCV_OK
            data = await self.ctrl.wait_notify("cmd_send", timeout=5.0)
            _LOGGER.info("Spec: response=%s", data.hex() if data else 'None')
            
            return {"ok": True, "sent": len(frame)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def ble_spec_send_flatbuffer(self, fb_data: bytes):
        """通过 BLE Spec FlatBuffers (MiOT加密通道) 发送命令并获取响应."""
        if not self.ctrl or not self.ctrl.client:
            return {"ok": False, "error": "not connected"}
        try:
            _LOGGER.info("Spec FB: sending %d bytes via MiOT encrypt: %s", len(fb_data), fb_data.hex())
            resp = await self.ctrl.send_spec_flatbuffer_command(fb_data)
            if resp:
                return {"ok": True, "response": resp.hex(), "len": len(resp)}
            return {"ok": False, "error": "no response"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
        """使用 bluetoothctl 强制断开蓝牙连接并重置适配器"""
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
                        if os.path.exists(power_file) and open(power_file).read().strip() == "1":
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
        last_refresh = time.time()
        last_notify = time.time()
        last_keepalive = time.time()

        while not self._stop_event.is_set():
            await self._process_commands()

            if not self.ctrl:
                break

            # 同时监听 MiOT (cmd_recv) 通知
            try:
                data = await asyncio.wait_for(
                    self.ctrl.wait_notify("cmd_recv"), timeout=2.0)
                if not self.ctrl:
                    break
                last_notify = time.time()
                if data[2] == 0x02 and len(data) >= 4:
                    await self._handle_inline_data(data)
                elif data[2] == 0x00 and len(data) >= 6:
                    await self._handle_multiframe(data)
                continue
            except asyncio.TimeoutError:
                now = time.time()
                if now - last_refresh > self.config.server.settings_refresh_interval:
                    await self._refresh_settings()
                    last_refresh = now
                if now - last_keepalive > 10:
                    if self.ctrl and self.ctrl.client and self.ctrl.client.is_connected:
                        try:
                            await self.ctrl.client.read_gatt_char(CHAR_FW_VERSION)
                            last_keepalive = now
                        except Exception:
                            pass
                    else:
                        if self.ctrl is None or not self.ctrl.client or not self.ctrl.client.is_connected:
                            if now - last_keepalive > 30:
                                raise ConnectionError('BLE disconnected via keepalive')
                if now - last_notify > 60:
                    client = self.ctrl.client if self.ctrl else None
                    if not client or not client.is_connected:
                        _LOGGER.warning("BLE connection lost, triggering reconnect")
                        raise ConnectionError("BLE disconnected")
                continue
            except Exception as e:
                _LOGGER.warning("BLE notification error: %s", e)
                raise

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
            await asyncio.sleep(0.1)
        if fail_count == 14:
            _LOGGER.warning("All PIID reads failed, BLE channel may be broken")
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
                elif cmd_type == "protocol":
                    await self._handle_protocol_extend_command(cmd_data, cmd_future)
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
                        self._publish_port(PORT_NAMES[piid], PORT_DEFAULT)
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": True, "value": new_val})
        except Exception as e:
            _LOGGER.error("Port command error: %s", e)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({"ok": False, "error": str(e)})

    async def _handle_protocol_extend_command(self, cmd_data, cmd_future):
        """处理协议开关命令 (PIID 21, 对齐米家 setProtocolExtend).
        
        cmd_data: {"port": "c1", "protocol": "pd"}  toggle 指定协议
                  {"switches": {port: {pd: bool,...}}}  批量设置
                  {"value": int}  直接写原始值
        """
        try:
            if "value" in cmd_data:
                new_val = cmd_data["value"]
            elif "switches" in cmd_data:
                new_val = ChargerState.encode_protocol_extend(cmd_data["switches"])
            elif "port" in cmd_data and "protocol" in cmd_data:
                cur = self.state.protocol_switches
                port = cmd_data["port"]
                proto = cmd_data["protocol"]
                if port in cur and proto in cur[port]:
                    cur[port][proto] = not cur[port][proto]
                new_val = ChargerState.encode_protocol_extend(cur)
            else:
                raise ValueError("Invalid protocol_extend command")

            # BLE Spec TLV 格式发送 PIID21 (替代 MiOT 4-byte SET)
            # 帧: [0x2000|prop_size:2B LE][msg_id:2B LE][count:1B][siid:1B][piid:2B LE][len:2B LE][value:4B LE]
            prop = struct.pack('<B', 2) + struct.pack('<H', 21) + struct.pack('<H', 4) + struct.pack('<I', new_val)
            tlv = struct.pack('<H', len(prop) | 0x2000) + struct.pack('<H', 0) + struct.pack('<B', 1) + prop
            _LOGGER.info("Protocol extend via BLE Spec TLV: value=0x%X tlv=%s", new_val, tlv.hex())
            resp = await self.ctrl.send_spec_flatbuffer_command(tlv)
            if resp:
                _LOGGER.info("Protocol extend response: %s", resp.hex())
            
            await self.state.update_protocol_extend(new_val)
            _invalidate()
            self._publish_settings(retain=True)
            if cmd_future and not cmd_future.done():
                cmd_future.set_result({
                    "ok": True, "value": new_val,
                    "switches": self.state.protocol_switches
                })
        except Exception as e:
            _LOGGER.error("Protocol extend error: %s", e)
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
        
        # BLE Spec 0f 20 frames: 仅记录日志，不做端口更新
        # (subscribeMessages 在 TinyPluginHost 中是 NO-OP,
        #  BLE Spec 自发的 32-bit 端口推送在 MiOT 模式下不可用)
        if pt[0:2] == b'\x0f\x20':
            _LOGGER.debug("BLESpec frame: piid=%d", piid)
            return
        
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
