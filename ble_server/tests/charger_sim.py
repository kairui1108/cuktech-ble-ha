"""虚拟充电器（设计文档 §7 P1：controller 层实现，回放 fixtures 驱动）。

它是什么、不是什么
------------------
**是**一台"从 BLE 链路往下全部虚拟"的设备：`VirtualChargerController` 继承真实的
`CuktechBLEController`，只替换四件事——连接、读设备信息、认证握手、通知通道的
入站管道。因此下面这些**生产代码一行不改、真实执行**：

- `send_miot_command()` 的 TLV 编码与响应解析；
- `_encrypt()/_send_encrypted()` 的 AES-CCM(app_key/app_iv) 与 CMD_SEND 握手；
- `decrypt()` 的 AES-CCM(dev_key/dev_iv) + `it` 回绕跟踪；
- 内联/多帧分流、`_forward_push` → `BLEManager._process_decrypted_frame` 的
  端口解码、会话/能量/历史落库全链路。

**不是**"把明文塞进状态"的假替身：帧是**真加密**的，几何是**真几何**（见
`tests/fixtures/README.md` §2 从 btsnoop 记录出来的线上布局），所以一旦生产侧改了
帧格式、加密参数或解析偏移，这些测试会直接红。

帧几何（与 fixtures 记录的真机一致）
-----------------------------------
    notify 帧 = [ctr_lo][ctr_hi][0x02][msg_type] + it_le16 + AES-CCM(dev) + tag(4)

请求（app→device）走同一条路径的反方向：`it_le16 + AES-CCM(app)`。

为什么需要它
------------
只有一台真机时，"多台真实充电器并发认证/推送"无法验证（§9 风险 5）。虚拟充电器
让 P1/P2 的软件全链路（注册/路由/SSE/DB/限额）在没有第二台硬件的前提下可测，且
`controller_factory` 注入点本身也被覆盖——它就是生产代码里那个注入口。
"""
from __future__ import annotations

import asyncio
import logging
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from cuktech_ble.controller import (  # noqa: E402
    CHAR_AUTH_CTRL, CHAR_AUTH_DATA, CHAR_CMD_RECV, CHAR_CMD_SEND,
    CHAR_DEVICE_INFO, CHAR_FW_VERSION, CuktechBLEController,
)
from cuktech_ble.protocol import (  # noqa: E402
    PORT_BITS, READABLE_SETTINGS_PIIDS, mac_str_to_bytes,
)

try:  # AES-CCM 是本模块的全部意义所在；缺依赖时明确报错而不是静默降级
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
except ImportError as e:  # pragma: no cover - venv 里一定有
    raise RuntimeError("charger_sim 需要 cryptography（AES-CCM）") from e


#: 虚拟会话密钥。真机的密钥是认证握手用 ECDH+HMAC 派生的，这里固定值即可——
#: 它只用于"加密方向必须真实"这件事，不承担安全性。
SIM_SESSION_KEYS = {
    "dev_key": bytes.fromhex("11" * 16),
    "dev_iv": bytes.fromhex("22" * 4),
    "app_key": bytes.fromhex("33" * 16),
    "app_iv": bytes.fromhex("44" * 4),
}

#: 端口 PIID（c1/c2/c3/a）与设置 PIID16 的端口掩码位（与生产 PORT_BITS 同源）
PORT_PIIDS = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
MASK_PIID = 16


# ────────────────────────── 设备侧加解密 ──────────────────────────

def sim_encrypt_dev(session_keys: dict, it: int, plaintext: bytes) -> bytes:
    """设备→app 方向加密：返回 `it_le16 + AES-CCM(dev_key, dev_iv)`。

    nonce 与生产 `decrypt()` 完全对齐：`dev_iv(4) + 0^4 + it_le32`。
    真机帧里只带 `it` 的低 16 位，这里也照做（跨 65535 时生产侧自己进位）。
    """
    nonce = session_keys["dev_iv"] + b"\x00" * 4 + struct.pack("<I", it)
    ct = AESCCM(session_keys["dev_key"], tag_length=4).encrypt(nonce, plaintext, None)
    return struct.pack("<H", it & 0xFFFF) + ct


def sim_decrypt_app(session_keys: dict, data: bytes) -> bytes:
    """app→device 方向解密：`it_le16 + AES-CCM(app_key, app_iv)`。"""
    it = struct.unpack("<H", data[:2])[0]
    nonce = session_keys["app_iv"] + b"\x00" * 4 + struct.pack("<I", it)
    return AESCCM(session_keys["app_key"], tag_length=4).decrypt(nonce, data[2:], None)


# ────────────────────────── 设备本体（纯状态机，无 IO）──────────────────────────

class VirtualCharger:
    """一台虚拟充电器的设备侧状态机：端口读数、设置项、请求应答。

    只做"值域内的确定性计算"，不碰 asyncio / BLE / 时钟——便于单测直接比对帧字节。
    """

    def __init__(self, *, seed: int | None = None, model: str = "CUKTECH-VIRTUAL",
                 firmware: str = "sim-1.0.0"):
        self.model = model
        self.firmware = firmware
        self._rnd = random.Random(seed)
        # 端口读数（真机帧里就是这几个字节：in_use / code / 电流×10mA / 电压×0.1V）
        self.ports = {piid: {"in_use": 0, "code": 0, "current": 0.0, "voltage": 0.0}
                      for piid in PORT_PIIDS}
        # 设置项：PIID16 = 端口开关掩码（bit0=c1 … bit3=a），其余给 0
        self.settings = {piid: 0 for piid in READABLE_SETTINGS_PIIDS}
        self.settings[MASK_PIID] = 0x0F
        self.enabled = {name: True for name in PORT_PIIDS.values()}

    # ── 设备状态驱动（测试用）──
    def set_port(self, piid: int, *, voltage: float, current: float,
                 in_use: bool | None = None, code: int = 0x07):
        """设置某端口的实测读数；`in_use=None` 时按 V/I 推导。"""
        self.ports[piid] = {
            "in_use": int(bool(voltage > 0 or current > 0) if in_use is None else in_use),
            "code": code,
            "current": current,
            "voltage": voltage,
        }

    def unplug(self, piid: int):
        self.set_port(piid, voltage=0.0, current=0.0, in_use=False, code=0)

    def mask(self) -> int:
        return self.settings[MASK_PIID]

    # ── 帧构造（设备→app 明文；加密由调用方做）──
    def port_push_plaintext(self, piid: int) -> bytes:
        """端口推送明文：`decode_port` 读最后 4 字节，分发器读 pt[4]/pt[7]。"""
        st = self.ports[piid]
        body = bytes([
            int(bool(st["in_use"])),
            st["code"] & 0xFF,
            min(255, max(0, int(round(st["current"] * 10)))),     # ×10 mA
            min(255, max(0, int(round(st["voltage"] * 10)))),     # ×0.1 V
        ])
        return bytes([16, 0x20, 0x00, 0x00, 0x04, 0x01, 2, piid,
                      0x00, 0x00, 0x00, 0x00]) + body

    def get_reply_plaintext(self, siid: int, piid: int) -> bytes:
        """GET 应答明文（`_recv_get_response` 的布局：err@9-10, len@11, type@12, val@13..）。"""
        if piid in PORT_PIIDS:      # 端口 GET：最后 4 字节必须是端口读数（verify_port 要 decode）
            st = self.ports[piid]
            return bytes([17, 0x20, 0x00, 0x00, 0x03, 0x01, siid & 0xFF, piid,
                          0x00, 0x00, 0x00, 4, 5]) + bytes([
                              int(bool(st["in_use"])), st["code"] & 0xFF,
                              min(255, int(round(st["current"] * 10))),
                              min(255, int(round(st["voltage"] * 10)))])
        value = int(self.settings.get(piid, 0)) & 0xFFFFFFFF
        return bytes([17, 0x20, 0x00, 0x00, 0x03, 0x01, siid & 0xFF, piid,
                      0x00, 0x00, 0x00, 4, 5]) + value.to_bytes(4, "little")

    def ack_plaintext(self, siid: int, piid: int) -> bytes:
        """SET 的 ACK（真机对 SET 基本只回 ACK，不回 Result）。"""
        return bytes([8, 0x20, 0x00, 0x00, 0x01, 0x01, siid & 0xFF, piid])

    # ── 请求处理 ──
    def handle_request(self, plaintext: bytes) -> list[bytes]:
        """处理一条 app→device 明文请求，返回要回送的明文帧列表。"""
        if len(plaintext) < 11:
            return []
        opcode = plaintext[4]
        siid = plaintext[6]
        piid = plaintext[7] | (plaintext[8] << 8)
        if opcode == 0x00:                      # SET
            tl = plaintext[9] | (plaintext[10] << 8)
            n = tl & 0xFF
            raw = plaintext[11:11 + n]
            value = int.from_bytes(raw, "little") if raw else 0
            self._apply_set(piid, value)
            return [self.ack_plaintext(siid, piid)]
        return [self.get_reply_plaintext(siid, piid)]   # GET

    def _apply_set(self, piid: int, value: int) -> None:
        if piid == MASK_PIID:
            self.settings[MASK_PIID] = value & 0x0F
            for name, bit in PORT_BITS.items():
                self.enabled[name] = bool(self.settings[MASK_PIID] & (1 << bit))
            return
        if piid in PORT_PIIDS:
            self.enabled[PORT_PIIDS[piid]] = bool(value)
        if piid in self.settings:
            self.settings[piid] = int(value)


# ────────────────────────── BLE 客户端替身（transport 层）──────────────────────────

class _VirtualBleClient:
    """`bleak.BleakClient` 的最小替身：只实现 controller 真正用到的三件事。

    它承担"线上链路"的角色：把 controller 写出去的数据交给虚拟设备，把设备的回帧
    塞回通知队列。这样 `_send_encrypted` 的 CMD_SEND 握手（RCV_RDY/RCV_OK）与
    push 的 ACK 都被真实执行。
    """

    def __init__(self, controller: "VirtualChargerController"):
        self._ctrl = controller
        self.is_connected = True
        self.mtu_size = 185

    async def read_gatt_char(self, char):
        """与 `bleak.BleakClient` 对齐的最小读口。

        `ble_manager._connect_and_run()` 的 60s 静默链路探测会
        `await client.read_gatt_char(CHAR_FW_VERSION)`；替身缺这个方法时抛的是
        `AttributeError`，被外层 `except Exception: lost = True` 吃掉 → 测试看到
        的是"设备掉线 → 触发分级恢复"，而不是"替身少了方法"，排障方向直接跑偏。
        """
        if char == CHAR_FW_VERSION:
            return self._ctrl.charger.firmware.encode("ascii") + b"\x00"
        return b""

    def _queue(self, name: str) -> asyncio.Queue:
        q = self._ctrl._notify_queues.get(name)
        if q is None:
            q = self._ctrl._notify_queues[name] = asyncio.Queue(maxsize=1024)
        return q

    async def start_notify(self, char, handler):
        # 生产 `connect()` 会订阅 5 个通道；这里只需保证队列存在
        self._ctrl._notify_handler = handler

    async def stop_notify(self, char):
        return None

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def write_gatt_char(self, char, data, response=False):
        if char == CHAR_CMD_SEND:
            await self._handle_cmd_send(bytes(data))
        elif char == CHAR_CMD_RECV:
            # 推送 ACK（00000300 / 00000101 / 00000100）——设备侧无需回应
            return None
        elif char == CHAR_AUTH_DATA:
            return None
        return None

    async def _handle_cmd_send(self, data: bytes) -> None:
        """CMD_SEND 三帧握手：头部 → RCV_RDY；数据帧 → 设备应答 + RCV_OK。"""
        cmd_send = self._queue("cmd_send")
        if data == bytes([0x00, 0x00, 0x00, 0x00, 0x01, 0x00]):
            cmd_send.put_nowait(bytes([0x00, 0x00, 0x01, 0x01]))     # RCV_RDY
            return
        if len(data) < 3 or data[0:2] != bytes([0x01, 0x00]):
            return
        plaintext = sim_decrypt_app(self._ctrl._session_keys, data[2:])
        self._ctrl.request_count += 1      # 文档承诺的"处理过的请求明文帧数"
        replies = self._ctrl.charger.handle_request(plaintext)
        recv = self._queue("cmd_recv")
        for pt in replies:
            recv.put_nowait(self._ctrl.frame_for(pt))
        cmd_send.put_nowait(bytes([0x00, 0x00, 0x01, 0x00]))         # RCV_OK


# ────────────────────────── controller（注入点）──────────────────────────

class VirtualChargerController(CuktechBLEController):
    """controller 层的虚拟充电器：`Server(controller_factory=...)` 注入用。"""

    def __init__(self, mac: str, token: str, *, charger: VirtualCharger | None = None,
                 push_interval: float = 0.05, session_keys: dict | None = None):
        # 不调 super().__init__()：它会 require_runtime_dependencies()（要 bleak 真装）。
        # 这里显式摆出 controller 契约要求的全部属性，其余方法全部继承真实现。
        self.mac = mac
        self.token = token
        self.mac_bytes = mac_str_to_bytes(mac)
        self.client = _VirtualBleClient(self)
        self.authenticated = False
        self._notify_queues = {}
        self._notify_handler = None
        self._disconnected = False
        self._send_it = 0
        self._dev_it_hi = 0
        self._last_dev_it_lo = 0
        self.device_model = ""
        self.firmware_version = ""
        self._miot_seq = 1
        self._session_keys = session_keys or dict(SIM_SESSION_KEYS)
        self.init_push_frames = []
        self.on_port_push = None
        self._mtu = 185
        self.charger = charger or VirtualCharger()
        self.push_interval = push_interval
        self.device_it = 0
        self._push_task: asyncio.Task | None = None
        # 排障/断言用：本 controller 处理过的请求明文帧数
        self.request_count = 0

    # ── controller 契约：连接/信息/认证 ──
    async def connect(self):
        for name in ("cmd_recv", "cmd_send", "dev_info", "auth_data", "auth_ctrl"):
            self._notify_queues.setdefault(name, asyncio.Queue(maxsize=1024))
        self.client.is_connected = True
        return True

    async def read_device_info(self):
        self.device_model = self.charger.model
        self.firmware_version = self.charger.firmware

    async def authenticate(self):
        # 真机的认证是 ECDH+HMAC 握手；虚拟设备直接进入会话（密钥固定）
        self.authenticated = True
        self._send_it = 0
        self._dev_it_hi = 0
        self._last_dev_it_lo = 0
        self._miot_seq = 1
        self._start_push_loop()
        return True

    async def stop_all_notifications(self):
        await self._stop_push_loop()
        for name in ("auth_ctrl", "auth_data", "cmd_send", "cmd_recv", "dev_info"):
            q = self._notify_queues.pop(name, None)
            if q:
                while not q.empty():
                    q.get_nowait()

    async def disconnect(self):
        await self.stop_all_notifications()
        self.client.is_connected = False

    # ── 帧出口 ──
    def frame_for(self, plaintext: bytes, *, msg_type: int = 0x02) -> bytes:
        """把一条设备侧明文封成真机形状的 notify 帧（加密 + 4 字节头）。"""
        env = sim_encrypt_dev(self._session_keys, self.device_it, plaintext)
        self.device_it += 1
        return bytes([0x00, 0x00, 0x02, msg_type]) + env

    def push_port(self, piid: int) -> None:
        """主动推一帧端口读数（真机就是这么上报的，1Hz 左右）。"""
        q = self._notify_queues.get("cmd_recv")
        if q is not None:
            q.put_nowait(self.frame_for(self.charger.port_push_plaintext(piid)))

    # ── 推送循环 ──
    def _start_push_loop(self) -> None:
        if self._push_task is None or self._push_task.done():
            self._push_task = asyncio.ensure_future(self._push_loop())

    async def _stop_push_loop(self) -> None:
        task, self._push_task = self._push_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                # 推送循环不该抛；真抛了（QueueFull、状态机报错）必须留痕，
                # 否则"虚拟设备静默停推"在上层只表现为一个无因果的超时。
                logging.getLogger(__name__).warning("虚拟设备推送循环异常退出: %r", e)

    async def _push_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.push_interval)
                for piid in PORT_PIIDS:
                    self.push_port(piid)
        except asyncio.CancelledError:
            raise


# ────────────────────────── 装配辅助 ──────────────────────────

def make_controller_factory(*, push_interval: float = 0.05, chargers=None):
    """返回给 `Server(controller_factory=...)` 用的工厂：每台 MAC 一台虚拟充电器。

    `chargers` 可按 MAC 覆写设备状态（默认一台全空闲的 4 口设备）。
    """
    chargers = dict(chargers or {})
    created: dict[str, VirtualChargerController] = {}

    def factory(mac: str, token: str):
        # seed 必须由 MAC 稳定派生：按 `len(created)` 取会让"同一台设备"在重建
        # （restart_device / per-device 重建用例会再调一次工厂）后换一套随机数，
        # 断言读数的用例就变成看运气。
        stable_seed = int(mac.replace(":", ""), 16) % (2 ** 32)
        ctrl = VirtualChargerController(
            mac, token, charger=chargers.get(mac, VirtualCharger(seed=stable_seed)),
            push_interval=push_interval)
        created[mac] = ctrl
        return ctrl

    factory.created = created        # 测试可直接拿 controller 断言/驱动设备
    return factory
