"""CUKTECH BLE Controller - Core BLE connection and command handling."""
import asyncio
import hashlib
import hmac
import logging
import secrets
import struct
import time

try:
    from bleak import BleakClient
except ImportError:
    BleakClient = None

try:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.hmac import HMAC as CryptoHMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
except ImportError:
    HKDF = None
    CryptoHMAC = None
    hashes = None
    default_backend = None
    AESCCM = None

from .protocol import (
    DEVICE_MAC, DEVICE_TOKEN,
    HANDLE_DEVICE_INFO, HANDLE_AUTH_CTRL, HANDLE_AUTH_DATA,
    HANDLE_CMD_SEND, HANDLE_CMD_RECV, HANDLE_FW_VERSION,
    CHAR_DEVICE_INFO, CHAR_AUTH_CTRL, CHAR_AUTH_DATA,
    CHAR_CMD_SEND, CHAR_CMD_RECV, CHAR_FW_VERSION,
    SIID_CHARGER, PIID_NAMES, PIID_DISPLAY, PORT_BITS,
    PROTOCOL_NAMES, PD_FIXED_VOLTAGES, PDO_KIND_BY_HIGH_BYTE,
    mac_str_to_bytes, require_runtime_dependencies,
)

_LOGGER = logging.getLogger("cuktech_ble")


class AuthConnectionError(ConnectionError):
    pass


# ============================================================
# BLE 控制器
# ============================================================
class CuktechBLEController:
    """CUKTECH 充电器 BLE 直连控制器。"""

    def __init__(self, mac=DEVICE_MAC, token=DEVICE_TOKEN):
        require_runtime_dependencies()
        self.mac = mac
        self.token = token
        self.mac_bytes = mac_str_to_bytes(mac)
        self.client = None
        self.authenticated = False
        self._notify_queues = {}
        self._send_it = 0
        self.device_model: str = ""
        self.firmware_version: str = ""
        self._miot_seq = 1
        self._session_keys = None
        self.init_push_frames = []  # 认证后初始推送的 inline 帧

    def _make_notify_handler(self, name):
        """创建通知回调函数 (基于队列，避免竞态条件)。"""
        if name not in self._notify_queues:
            self._notify_queues[name] = asyncio.Queue(maxsize=1024)

        queue = self._notify_queues[name]

        def handler(sender, data):
            queue.put_nowait(data)

        return handler

    async def wait_notify(self, name, timeout=5.0):
        """等待指定通道的通知数据。"""
        queue = self._notify_queues.get(name)
        if not queue:
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def get_pending_notify(self, name):
        """Non-blocking: get one pending notification if available, else None."""
        queue = self._notify_queues.get(name)
        if not queue:
            return None
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _recv_auth_response(self, channel, label="数据"):
        """接收认证响应数据，自动处理内联和多帧两种格式。

        内联格式: 00 00 02 XX [data]  (小数据直接发送)
        多帧格式: 00 00 00 XX count_lo count_hi  (大数据分帧发送)
        """
        data = await self.wait_notify(channel, timeout=3.0)
        if not data or len(data) < 4:
            return None

        # 判断格式: byte[2] == 0x02 → 内联, byte[2] == 0x00 → 多帧头
        if data[2] == 0x02:
            # 内联格式: 0000 02 XX [data]
            payload = data[4:]
            # 发送 ACK
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
            return payload

        elif data[2] == 0x00 and len(data) >= 6:
            # 多帧头: 0000 00 XX count_lo count_hi
            frame_count = data[4] + 0x100 * data[5]
            data_id = data[3]
            _LOGGER.debug("Multi-frame auth response: %d frames, ID=0x%02x", frame_count, data_id)

            # 发送 RCV_RDY
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x01, 0x01]), response=False)

            # 接收所有数据帧
            received = b''
            for i in range(frame_count):
                frame = await self.wait_notify(channel, timeout=3.0)
                if not frame:
                    _LOGGER.warning("Frame %d/%d timeout during auth", i+1, frame_count)
                    break
                # 帧格式: [frm_lo][frm_hi][data...]
                frm = frame[0] + 0x100 * frame[1]
                received += frame[2:]

            # 发送 RCV_OK
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
            return received

        else:
            _LOGGER.warning("Unknown auth response format: data[2]=0x%02x, len=%d, data=%s",
                           data[2], len(data), data.hex())
            return None

    async def connect(self):
        """连接到设备。"""
        _LOGGER.info("Connecting to %s...", self.mac)
        self.client = BleakClient(self.mac)
        await self.client.connect()
        try:
            await self.client._acquire_mtu()
        except (AttributeError, Exception):
            _LOGGER.warning("MTU negotiation failed, using default MTU=%d", self.client.mtu_size)

        # 清理可能残留的通知队列
        for q in self._notify_queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # 订阅通知通道 — 对齐米家时序:
        # 第一批: cmd_recv, cmd_send, dev_info
        for char, name in [
            (CHAR_CMD_RECV, "cmd_recv"),
            (CHAR_CMD_SEND, "cmd_send"),
            (CHAR_DEVICE_INFO, "dev_info"),
        ]:
            try:
                await self.client.start_notify(char, self._make_notify_handler(name))
            except Exception as e:
                _LOGGER.warning("Failed to subscribe %s: %s", name, e)

        # 第二批: auth_data (密钥交换前订阅)
        try:
            await self.client.start_notify(CHAR_AUTH_DATA, self._make_notify_handler("auth_data"))
        except Exception as e:
            _LOGGER.warning("Failed to subscribe auth_data: %s", e)

        _LOGGER.info("Notification channels subscribed (deferred auth_ctrl)")

        return True

    async def stop_all_notifications(self):
        """取消所有已订阅的 GATT 通知通道。

        在断开连接前调用，避免 BlueZ GATT 缓存积累错误的 CCCD
        描述符状态，导致快速重连时认证失败。
        """
        chars_to_stop = []
        for name in ("auth_ctrl", "auth_data", "cmd_send", "cmd_recv", "dev_info"):
            if name in self._notify_queues:
                chars_to_stop.append(name)

        for name in chars_to_stop:
            char_uuid = {
                "auth_ctrl": CHAR_AUTH_CTRL,
                "auth_data": CHAR_AUTH_DATA,
                "cmd_send": CHAR_CMD_SEND,
                "cmd_recv": CHAR_CMD_RECV,
                "dev_info": CHAR_DEVICE_INFO,
            }.get(name)
            if char_uuid and self.client and self.client.is_connected:
                try:
                    await self.client.stop_notify(char_uuid)
                except Exception:
                    pass
            # 清空对应队列
            q = self._notify_queues.pop(name, None)
            if q:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

    async def disconnect(self):
        """断开连接。"""
        await self.stop_all_notifications()
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            _LOGGER.info("Disconnected")

    async def read_device_info(self):
        """读取设备信息 (无需认证)，存储到 self.device_model / self.firmware_version。"""
        _LOGGER.info("Reading device info...")

        # 订阅设备信息通知
        await self.client.start_notify(CHAR_DEVICE_INFO, self._make_notify_handler("dev_info"))

        # 查询协议版本
        await self.client.write_gatt_char(CHAR_DEVICE_INFO, bytes([0x00]), response=False)
        data = await self.wait_notify("dev_info")
        if data:
            version = data[1] if len(data) > 1 else 0
            sub_ver = data[2] if len(data) > 2 else 0
            _LOGGER.info("Protocol version: v%d.%d", version, sub_ver)

        # 查询芯片信息
        await self.client.write_gatt_char(CHAR_DEVICE_INFO, bytes([0x03]), response=False)
        data = await self.wait_notify("dev_info")
        if data and len(data) > 2:
            chip_name = data[2:2 + data[1]].decode("ascii", errors="replace")
            self.device_model = f"njcuk.fitting.ad1204_{chip_name}"
            _LOGGER.info("Chip (model): %s -> %s", chip_name, self.device_model)

        # 读取固件版本
        try:
            fw_data = await self.client.read_gatt_char(CHAR_FW_VERSION)
            fw_str = fw_data.rstrip(b'\x00').decode("ascii", errors="replace")
            self.firmware_version = fw_str
            _LOGGER.info("Firmware: %s", fw_str)
        except Exception as e:
            self.firmware_version = ""
            _LOGGER.warning("Failed to read firmware version: %s", e)

        await self.client.stop_notify(CHAR_DEVICE_INFO)

    async def authenticate(self):
        """执行 MiOT BLE 登录认证。

        协议流程 (基于 miauth 库和 BLE 日志验证):
        Phase A: 设备初始化 (0xa4)
        Phase B: 密钥交换 (CMD_LOGIN=0x24)
          1. 发送 CMD_SEND_KEY, 传输 16 字节随机密钥
          2. 接收设备 16 字节随机密钥
          3. HKDF 派生会话密钥 (TOKEN + salt)
          4. 验证设备 HMAC, 发送我方 HMAC
          5. 收到 0x21 = 登录成功
        """
        return await self._try_authenticate()

    async def _try_authenticate(self):
        """执行单次认证尝试。"""
        _LOGGER.info("[*] 开始 MiOT BLE 认证...")

        # 认证通道已在 connect() 中预订阅

        # ---- Phase A: 设备初始化 ----
        _LOGGER.info("  [1/5] 设备初始化 (0xa4)...")
        # 清空可能残留的通知数据
        queue = self._notify_queues.get("auth_data")
        if queue:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        await self.client.write_gatt_char(CHAR_AUTH_CTRL, bytes([0xa4]), response=False)
        init_resp = await self.wait_notify("auth_data", timeout=3.0)
        if not init_resp:
            _LOGGER.warning("  [!] 未收到初始化响应")
            return False
        _LOGGER.debug("  初始化响应: %s", init_resp.hex())

        # 协议协商回传: byte[2] += 1 (04→05)
        ack = bytearray(init_resp)
        if len(ack) >= 3:
            ack[2] = ack[2] + 1
        await self.client.write_gatt_char(CHAR_AUTH_DATA, bytes(ack), response=False)

        # 接收设备密钥交换数据 (本设备发送 240 字节 0xf2 占位)
        key_data = await self.wait_notify("auth_data", timeout=5.0)
        if key_data:
            _LOGGER.debug("  密钥交换数据: %d bytes", len(key_data))

        # 检测: 充电器状态机不同步 — 发送了重复的 init 响应而非 key exchange
        # 正常 key exchange: byte[2] == 0x04, len >= 20
        # 异常: byte[2] == 0x04 但 len < 20 (重复 init 响应 0000040006f2)
        is_desync = (
            key_data
            and len(key_data) < 20
            and len(key_data) >= 3
            and key_data[2] == 0x04
        )
        if is_desync:
            _LOGGER.warning("  [!] 设备状态不同步 (key_data=%d bytes, byte[2]=0x%02x), 等待设备恢复...",
                            len(key_data), key_data[2])
            # 等待设备发完 key exchange 数据（可能在后续通知中）
            # 使用 deadline 而非独立 timeout，避免第一次超时就 break
            deadline = asyncio.get_running_loop().time() + 12
            for _ in range(3):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    extra = await self.wait_notify("auth_data", timeout=remaining)
                    if extra and len(extra) >= 20 and extra[2] == 0x04:
                        _LOGGER.info("  收到延迟的 key exchange 数据: %d bytes", len(extra))
                        key_data = extra
                        break
                except asyncio.TimeoutError:
                    continue  # retry until deadline
                except Exception:
                    break
            else:
                _LOGGER.warning("  [!] 设备未恢复，放弃本次认证")
                return False
        elif not key_data:
            _LOGGER.warning("  [!] 未收到密钥交换数据")
            return False

        # 回传相同长度的占位数据 (使用 0xf2 与 BLE 日志一致)
        pad_len = max(0, len(key_data) - 4)
        placeholder = bytes([0x00, 0x00, 0x05, 0x01]) + bytes([0xf2] * pad_len)
        await self.client.write_gatt_char(CHAR_AUTH_DATA, placeholder, response=False)

        # 等待设备处理占位符，两次 drain 消费残留通知
        for drain_wait in (0.3, 0.3):
            await asyncio.sleep(drain_wait)
            queue = self._notify_queues.get("auth_data")
            if queue:
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

        # ---- Phase B: 登录认证 (CMD_LOGIN) ----
        # 对齐米家: 订阅 auth_ctrl (0x0010) 在密钥交换之后、CMD_LOGIN 之前
        try:
            await self.client.start_notify(CHAR_AUTH_CTRL, self._make_notify_handler("auth_ctrl"))
            _LOGGER.debug("  [2/5] Subscribed auth_ctrl (0x0010)")
        except Exception as e:
            _LOGGER.debug("  [2/5] auth_ctrl subscribe: %s", e)
        _LOGGER.info("  [2/5] 发送登录命令 (CMD_LOGIN=0x24)...")
        await asyncio.sleep(0.05)
        CMD_LOGIN = bytes([0x24, 0x00, 0x00, 0x00])
        await self.client.write_gatt_char(CHAR_AUTH_CTRL, CMD_LOGIN, response=False)

        # ---- 发送我方随机密钥 ----
        _LOGGER.info("  [3/5] 发送随机密钥...")
        rand_key = secrets.token_bytes(16)

        CMD_SEND_KEY = bytes([0x00, 0x00, 0x00, 0x0b, 0x01, 0x00])
        await self.client.write_gatt_char(CHAR_AUTH_DATA, CMD_SEND_KEY, response=False)

        # 等待 RCV_RDY（跳过残留的 key exchange 等数据）
        for _retry in range(5):
            data = await self.wait_notify("auth_data", timeout=3.0)
            if data == bytes([0x00, 0x00, 0x01, 0x01]):
                break
            if data:
                _LOGGER.debug("  [Phase B] skipped %d bytes (expecting RCV_RDY)", len(data))
        else:
            _LOGGER.warning("  [!] 未收到 RCV_RDY after retries")
            return False

        # 发送随机密钥 (带帧头 0100)
        await self.client.write_gatt_char(
            CHAR_AUTH_DATA, bytes([0x01, 0x00]) + rand_key, response=False)

        # 等待 RCV_OK
        data = await self.wait_notify("auth_data", timeout=3.0)
        if not data or data != bytes([0x00, 0x00, 0x01, 0x00]):
            _LOGGER.warning("  [!] 未收到 RCV_OK, got: %s", data.hex() if data else 'None')
            return False

        # ---- 接收设备随机密钥 ----
        _LOGGER.info("  [4/5] 接收设备响应...")
        dev_random = await self._recv_auth_response("auth_data", "设备密钥")
        if not dev_random or len(dev_random) < 16:
            _LOGGER.warning("  [!] 设备密钥无效: %d bytes", len(dev_random) if dev_random else 0)
            return False
        dev_random = dev_random[:16]  # 取前16字节

        # 接收设备 HMAC 信息 (32 字节)
        dev_hmac_info = await self._recv_auth_response("auth_data", "设备HMAC")
        if not dev_hmac_info or len(dev_hmac_info) < 32:
            _LOGGER.warning("  [!] 设备 HMAC 无效: %d bytes", len(dev_hmac_info) if dev_hmac_info else 0)
            return False
        dev_hmac_info = dev_hmac_info[:32]  # 取前32字节

        # ---- 计算会话密钥并验证 ----
        salt = rand_key + dev_random
        salt_inv = dev_random + rand_key

        # HKDF 派生密钥
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=salt,
            info=b"mible-login-info",
            backend=default_backend()
        ).derive(self.token)

        self._session_keys = {
            'dev_key': derived[0:16],
            'app_key': derived[16:32],
            'dev_iv': derived[32:36],
            'app_iv': derived[36:40],
        }
        _LOGGER.info("  会话密钥已派生")
        _LOGGER.debug("  app_key=%s app_iv=%s",
                      self._session_keys['app_key'].hex(),
                      self._session_keys['app_iv'].hex())

        # 验证设备 HMAC
        hmac_dev = CryptoHMAC(self._session_keys['dev_key'], algorithm=hashes.SHA256())
        hmac_dev.update(salt_inv)
        expected_dev_hmac = hmac_dev.finalize()

        if not hmac.compare_digest(expected_dev_hmac, dev_hmac_info):
            _LOGGER.error("  [!] 设备 HMAC 验证失败!")
            return False
        _LOGGER.info("  [+] 设备 HMAC 验证通过!")

        # 计算并发送我方 HMAC
        hmac_app = CryptoHMAC(self._session_keys['app_key'], algorithm=hashes.SHA256())
        hmac_app.update(salt)
        our_hmac = hmac_app.finalize()

        _LOGGER.info("  [5/5] 发送认证确认...")
        CMD_SEND_INFO = bytes([0x00, 0x00, 0x00, 0x0a, 0x01, 0x00])
        await self.client.write_gatt_char(CHAR_AUTH_DATA, CMD_SEND_INFO, response=False)

        # 等待 RCV_RDY
        data = await self.wait_notify("auth_data", timeout=3.0)
        if not data or data != bytes([0x00, 0x00, 0x01, 0x01]):
            _LOGGER.warning("  [!] 未收到 RCV_RDY, got: %s", data.hex() if data else 'None')
            return False

        # 发送 HMAC (32字节，在一帧内发送, 与 BLE 日志一致)
        frame = bytes([0x01, 0x00]) + our_hmac  # frame_num=1 + 32字节 HMAC
        await self.client.write_gatt_char(CHAR_AUTH_DATA, frame, response=False)

        # 等待 RCV_OK
        data = await self.wait_notify("auth_data", timeout=3.0)
        _LOGGER.debug("  Phase5 ACK: %s", data.hex() if data else 'None')

        # ---- 认证第二轮: 挑战-应答 + 第二轮 auth response ----
        # Frida 抓到的完整序列:
        # R: challenge (0x0d, 16B)  → W: keepalive
        # R: response  (0x0c, 32B)  → W: keepalive → W: ACK → R: RCV_RDY
        # W: second auth response (0x0c, 32B) → R: RCV_OK
        try:
            deadline = asyncio.get_event_loop().time() + 8.0
            challenge_data = None
            response_data = None

            while asyncio.get_event_loop().time() - deadline < 0:
                break

            # 读取 challenge + response + RCV_RDY
            while asyncio.get_event_loop().time() - deadline < 0:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                data = await self.wait_notify("auth_data", timeout=min(remaining, 3.0))
                if not data:
                    continue
                _LOGGER.debug("  Phase6 auth_data: %s", data.hex())

                # Challenge: 00 00 02 0d [16B]
                if len(data) >= 3 and data[2] == 0x0d:
                    challenge_data = data
                    _LOGGER.debug("  Phase6: challenge received (%dB)", len(data))
                    # 发送 keepalive
                    await self.client.write_gatt_char(
                        CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x03, 0x00]), response=False)

                # Response: 00 00 02 0c [32B]
                elif len(data) >= 3 and data[2] == 0x0c:
                    response_data = data
                    _LOGGER.debug("  Phase6: response received (%dB)", len(data))
                    # 发送 keepalive
                    await self.client.write_gatt_char(
                        CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x03, 0x00]), response=False)

                    # 发送 ACK
                    await self.client.write_gatt_char(
                        CHAR_AUTH_DATA, bytes([0x00, 0x00, 0x00, 0x0a, 0x01, 0x00]), response=False)

                # RCV_RDY: 00 00 01 01
                elif data == bytes([0x00, 0x00, 0x01, 0x01]):
                    _LOGGER.debug("  Phase6: RCV_RDY received")
                    break

                # RCV_OK: 00 00 01 00 (可能直接跳到 auth_ctrl)
                elif data == bytes([0x00, 0x00, 0x01, 0x00]):
                    _LOGGER.debug("  Phase6: RCV_OK, skipping to auth_ctrl")
                    break

            # 发送第二轮 auth response
            if response_data:
                # 构造第二轮 auth response: header=01 00, opcode=0c, data=响应
                auth_resp_payload = bytes([0x01, 0x00, 0x0c]) + response_data[3:]
                _LOGGER.debug("  Phase6: sending 2nd auth response (%dB)", len(auth_resp_payload))
                await self.client.write_gatt_char(
                    CHAR_AUTH_DATA, auth_resp_payload, response=False)

                # 等待 RCV_OK
                data = await self.wait_notify("auth_data", timeout=3.0)
                _LOGGER.debug("  Phase6: post-response ACK: %s", data.hex() if data else 'None')

        except asyncio.TimeoutError:
            _LOGGER.debug("  Phase6: timeout (may be OK)")
        except Exception as e:
            _LOGGER.debug("  Phase6: error %s (continuing)", e)

        # ---- 等待认证结果 ----
        result = await self.wait_notify("auth_ctrl", timeout=5.0)
        if result:
            frm = result[0]
            if frm == 0x21:
                _LOGGER.info("  [+] 认证成功! (Login OK)")
                self.authenticated = True
                await self._setup_cmd_channel()
            elif frm == 0x11:
                _LOGGER.info("  [+] 激活成功!")
                self.authenticated = True
                await self._setup_cmd_channel()
            elif frm == 0x23:
                _LOGGER.error("  [!] 登录失败 (Login Failed)")
            elif frm == 0x12:
                _LOGGER.error("  [!] 激活失败")
            else:
                _LOGGER.warning("  [?] 未知结果: 0x%02x", frm)
        else:
            _LOGGER.warning("  [!] 未收到认证结果")

        return self.authenticated

    async def _drain_device_push(self):
        """消耗设备认证后的自动推送数据。

        保存 inline 帧 (0x02) 到 init_push_frames 供主循环处理，
        不再丢弃端口数据。
        """
        self.init_push_frames = []
        _LOGGER.debug("Waiting for device init push...")
        push_count = 0
        start = asyncio.get_running_loop().time()
        max_drain_seconds = 6.0
        max_push_count = 60

        while True:
            if push_count >= max_push_count:
                _LOGGER.debug("Init push limit reached (%d), stopping drain", max_push_count)
                break
            if asyncio.get_running_loop().time() - start >= max_drain_seconds:
                _LOGGER.debug("Init push drain timeout (%.1fs), continuing", max_drain_seconds)
                break

            data = await self.wait_notify("cmd_recv", timeout=0.8)
            if not data:
                break
            push_count += 1

            if data[2] == 0x00 and len(data) >= 6:
                frame_count = min(data[4] + 0x100 * data[5], 100)
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
                deadline = time.monotonic() + 10.0
                for i in range(frame_count):
                    if time.monotonic() >= deadline:
                        break
                    frame = await self.wait_notify("cmd_recv", timeout=min(deadline - time.monotonic(), 3.0))
                    if not frame:
                        break
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
            elif data[2] == 0x02:
                # 保存 inline 帧供主循环处理（端口数据）
                self.init_push_frames.append(data)
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)

        _LOGGER.debug("Drained %d push messages, saved %d inline frames",
                      push_count, len(self.init_push_frames))

    # ---- 加密命令通道 ----

    def _encrypt(self, plaintext):
        """AES-CCM 加密 (发送方向: app_key + app_iv)。

        格式 (参考 miauth encrypt_uart):
          nonce = app_iv(4) + zeros(4) + send_it(4) = 12 bytes
          ciphertext = AESCCM(app_key, tag=4).encrypt(nonce, plaintext, None)
          输出 = it_lo + it_hi + ciphertext
        """
        it = self._send_it
        it_bytes = struct.pack('<I', it)
        nonce = self._session_keys['app_iv'] + b'\x00' * 4 + it_bytes

        aes_ccm = AESCCM(self._session_keys['app_key'], tag_length=4)
        ct = aes_ccm.encrypt(nonce, plaintext, None)

        self._send_it += 1
        return it_bytes[:2] + ct

    def decrypt(self, data):
        """AES-CCM 解密 (接收方向: dev_key + dev_iv)。

        输入格式: it_lo + it_hi + ciphertext
        nonce = dev_iv(4) + zeros(4) + it(2) + zeros(2) = 12 bytes
        """
        if len(data) < 6:  # 2 bytes it + 4 bytes minimum tag
            _LOGGER.warning("Decrypt data too short: %d bytes", len(data))
            return None

        it = data[:2]
        ct = data[2:]
        nonce = self._session_keys['dev_iv'] + b'\x00' * 4 + it + b'\x00' * 2

        try:
            aes_ccm = AESCCM(self._session_keys['dev_key'], tag_length=4)
            pt = aes_ccm.decrypt(nonce, ct, None)
            return pt
        except Exception as e:
            _LOGGER.warning("Decrypt failed: %s", e)
            return None

    @staticmethod
    def _extract_typed_u8(pt, type_idx, value_idx):
        """提取 1 字节属性值。

        某些属性返回的 type 码不一定是 0x10（如布尔类），
        对当前设置项统一按 value 位置取值，避免误判为 0。
        """
        if not pt or len(pt) <= value_idx:
            return None
        return pt[value_idx]

    async def _setup_cmd_channel(self):
        """认证成功后，初始化命令通道 (已在 connect 中预订阅)。"""
        self._send_it = 0
        self._miot_seq = 1  # MiOT 命令序列号 (从1开始)
        await asyncio.sleep(0.5)
        await self._drain_pending_pushes()
        _LOGGER.info("Command channel ready")

        # 等待并消耗设备认证后自动推送的数据
        await self._drain_device_push()

    async def _send_encrypted(self, plaintext):
        """通过 CMD_SEND 通道发送加密数据。

        协议流程 (从 BLE 日志分析):
        1. 写入头部 000000000100 到 CMD_SEND (发送 1 帧)
        2. 等待 00000101 (RCV_RDY)
        3. 写入 0100 + 加密数据 (帧号=1)
        4. 等待 00000100 (RCV_OK)
        """
        encrypted = self._encrypt(plaintext)

        # 发送头部: 告知设备我们要发送 1 帧数据
        header = bytes([0x00, 0x00, 0x00, 0x00, 0x01, 0x00])
        await self.client.write_gatt_char(CHAR_CMD_SEND, header, response=False)

        # 等待 RCV_RDY
        data = await self.wait_notify("cmd_send", timeout=3.0)
        if data != bytes([0x00, 0x00, 0x01, 0x01]):
            _LOGGER.warning("CMD_SEND no RCV_RDY: %s", data.hex() if data else 'None')
            return False

        # 发送数据帧 (帧号 0100 + 加密数据)
        frame = bytes([0x01, 0x00]) + encrypted
        await self.client.write_gatt_char(CHAR_CMD_SEND, frame, response=False)

        # 等待 RCV_OK
        data = await self.wait_notify("cmd_send", timeout=3.0)
        if data != bytes([0x00, 0x00, 0x01, 0x00]):
            _LOGGER.warning("CMD_SEND no RCV_OK: %s", data.hex() if data else 'None')
            return False

        return True

    async def _drain_pending_pushes(self):
        """快速消耗队列中积压的推送通知 (非阻塞)。"""
        drained = 0
        q = self._notify_queues.get("cmd_recv")
        if not q:
            return 0

        while True:
            try:
                data = q.get_nowait()
            except asyncio.QueueEmpty:
                break

            if data and len(data) >= 4 and data[2] == 0x02:
                # 内联推送: 发送 ACK
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
                drained += 1
            elif data and len(data) >= 6 and data[2] == 0x00:
                # 多帧: 限制总帧数和总超时
                frame_count = data[4] + 0x100 * data[5]
                if frame_count > 100:
                    _LOGGER.warning("Drain: %d frames exceeds limit, capping at 100", frame_count)
                    frame_count = 100
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
                deadline = time.monotonic() + 10.0  # 总超时 10s
                for _ in range(frame_count):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=min(remaining, 2.0))
                    except asyncio.TimeoutError:
                        break
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
                drained += 1

        if drained > 0:
            _LOGGER.debug("Drained %d pending pushes", drained)
        return drained

    @staticmethod
    def _build_miot_tlv(seq: int, siid: int, piid: int, value=None) -> bytes:
        """构建 MiOT TLV 命令 plaintext。

        帧格式 (对齐米家 SpecWriteChannelManager):
          [total_len:1B][0x20:1B][seq:1B][0x00:1B][opcode:1B][cnt:1B]
          [siid:1B][piid:2B LE][tl:2B LE][value:NB]
          tl = (type_id << 12) | byte_length
          type_id: 1=UINT8, 5=UINT32 (SpecValueType)
        """
        if value is not None:
            opcode = 0x00  # SET
            if value <= 0xFF:
                type_id = 1   # UINT8
                value_bytes = bytes([value & 0xFF])
            else:
                type_id = 5   # UINT32
                value_bytes = value.to_bytes(4, 'little')
        else:
            opcode = 0x02  # GET
            type_id = 1    # UINT8 (占位)
            value_bytes = bytes([0x00])

        byte_len = len(value_bytes)
        tl = (type_id << 12) | byte_len
        total_len = 11 + byte_len  # 固定帧头 11 字节 + 值长度

        return bytes([
            total_len & 0xFF, 0x20, seq, 0x00,
            opcode, 0x01, siid & 0xFF,
            piid & 0xFF, (piid >> 8) & 0xFF,
            tl & 0xFF, (tl >> 8) & 0xFF,
        ]) + value_bytes

    async def send_miot_command(self, siid, piid, value=None):
        """发送 MiOT BLE 命令并返回解析后的响应。

        MiOT/Spec TLV 帧格式 (对齐米家 SpecWriteChannelManager):
          frame_header: [tot_len:1B] [0x20:1B]   — 0x20xx, xx=总长度
          packet:       [frame_header:2B] [seq:1B] [0x00:1B] [opcode:1B] [cnt:1B]
          TLV entry:    [siid:1B] [piid:2B LE] [tl:2B LE] [value:NB]
          tl:           (type_id << 12) | byte_length
          type_id:      1=UINT8, 3=UINT16, 5=UINT32 (SpecValueType)

        响应格式:
          SET ACK:    [0x0b] [0x20] [seq]   [0x00] [0x01] [0x01] [siid] [piid]
          SET Result: [0x0f] [0x20] [seq]   [0x00] [0x04] [0x01] [siid] [piid] [0x00] [0x00] [len] [type] [value]
          GET Result: [0x11] [0x20] [seq]   [0x00] [0x03] [0x01] [siid] [piid] [0x00] [0x00] [len] [type] [value]

        v1.0.4+ 修复: 框架头长度使用实际包长，piid 改为 2 字节 LE，
        4 字节值使用 type_id=5 (UINT32) 而非 type_id=1 (UINT8)。
        """
        if not self.authenticated:
            _LOGGER.warning("Not authenticated, cannot send command")
            return None

        await self._drain_pending_pushes()

        seq = self._miot_seq
        self._miot_seq = (self._miot_seq + 1) & 0xFF

        plaintext = self._build_miot_tlv(seq, siid, piid, value)
        _LOGGER.debug("%s siid=%d piid=%d value=%s len=%d",
                      "SET" if value is not None else "GET",
                      siid, piid, hex(value) if value is not None else "None",
                      len(plaintext))

        if not await self._send_encrypted(plaintext):
            return None

        # 接收响应 (根据 opcode 区分)
        if value is not None:
            return await self._recv_set_response(siid, piid, timeout=8.0)
        else:
            return await self._recv_get_response(siid, piid, timeout=8.0)

    async def _recv_set_response(self, siid, piid, timeout=8.0):
        """接收 SET 命令的响应: 期望 ACK (B4=0x01) + Result (B4=0x04)。"""
        deadline = asyncio.get_running_loop().time() + timeout
        got_ack = False

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            data = await self.wait_notify("cmd_recv", timeout=min(remaining, 3.0))
            if not data or len(data) < 4:
                if got_ack:
                    break
                continue

            pt, _ = await self._try_decode_inline(data)
            if pt is None:
                continue

            b4 = pt[4]; pt_siid = pt[6] if len(pt) > 6 else -1; pt_piid = pt[7] if len(pt) > 7 else -1

            if b4 == 0x01 and pt_siid == (siid & 0xFF) and pt_piid == (piid & 0xFF):
                got_ack = True
                deadline = asyncio.get_running_loop().time() + 1.0
                continue
            elif b4 == 0x04 and pt_siid == (siid & 0xFF) and pt_piid == (piid & 0xFF):
                val = None
                # 值从 pt[11] 开始（经 CLI 实测验证）
                if len(pt) >= 12:
                    vlen = pt[11] if len(pt) > 11 else 1
                    if vlen >= 4 and len(pt) >= 15:
                        val = int.from_bytes(pt[11:15], 'little')
                    else:
                        val = pt[11]
                return {'piid': piid, 'value': val, 'raw': pt}

        if got_ack:
            _LOGGER.debug("SET acknowledged (ACK only)")
            return {'piid': piid, 'value': None, 'raw': None}
        _LOGGER.debug("SET no response")
        return None

    async def _recv_get_response(self, siid, piid, timeout=8.0):
        """接收 GET 命令的响应: 期望 B4=0x03, 值在 B12。"""
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            data = await self.wait_notify("cmd_recv", timeout=min(remaining, 3.0))
            if not data or len(data) < 4:
                break

            pt, _ = await self._try_decode_inline(data)
            if pt is None:
                continue

            b4 = pt[4]; pt_siid = pt[6] if len(pt) > 6 else -1; pt_piid = pt[7] if len(pt) > 7 else -1

            if b4 == 0x03 and pt_siid == (siid & 0xFF) and pt_piid == (piid & 0xFF):
                result_value = None
                if len(pt) >= 14:
                    val_len = pt[11] if len(pt) > 11 else 1
                    if val_len >= 4 and len(pt) >= 17:
                        result_value = int.from_bytes(pt[13:17], 'little')
                    else:
                        result_value = pt[13] if len(pt) > 13 else None
                return {'piid': piid, 'value': result_value, 'raw': pt}

        _LOGGER.debug("GET no response")
        return None

    async def _try_decode_inline(self, data):
        """解密并解析内联帧，返回 (plaintext,None) 或 (None,None) 或 ACK多帧后 (None,True)。"""
        if data[2] == 0x02 and len(data) >= 4:
            encrypted_payload = data[4:]
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False)
            pt = self.decrypt(encrypted_payload)
            return (pt, None) if pt and len(pt) >= 8 else (None, None)
        elif data[2] == 0x00 and len(data) >= 6:
            frame_count = min(data[4] + 0x100 * data[5], 100)
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False)
            for _ in range(frame_count):
                await self.wait_notify("cmd_recv", timeout=3.0)
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False)
            return None, True
        return None, None

    async def get_properties(self, props):
        """批量获取属性。props = [(siid, piid), ...]"""
        async def fetch_one(siid, piid):
            result = await self.send_miot_command(siid, piid)
            return (siid, piid), result.get('value') if result else None

        tasks = [fetch_one(siid, piid) for siid, piid in props]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        failed = 0
        for (siid, piid), value in results_list:
            if isinstance(value, Exception) or value is None:
                failed += 1
            else:
                results[(siid, piid)] = value
        if failed > 0:
            _LOGGER.warning("get_properties: %d/%d properties failed", failed, len(props))
        return results

