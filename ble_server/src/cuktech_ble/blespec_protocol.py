"""BLE Spec 协议栈 - 基于米家 SDK 源码逆向实现.

协议层次 (对应 SDK 类):
  React Native 层 → setPropertiesValue(mac, json)
  Java 层:
    m78 → 构建 FlatBuffers 消息 (eee.OooO00o → sae 编码器)
    bhe → BLE 通道/管道管理
    uhb → 协议版本协商 + j1a 帧封装
    xp  → BLE GATT 写入
    rv0 → BLE 连接认证管理

BLE Spec vs MiOT 对比:
  ┌──────────┬─────────────────┬──────────────────┐
  │          │ MiOT (旧)        │ BLE Spec (新)     │
  ├──────────┼─────────────────┼──────────────────┤
  │ 帧头      │ 0c 20           │ 0f 20 (响应)      │
  │ 数据编码   │ TLV 简单字节     │ 05 20 (设置帧)    │
  │ 端口数据   │ [use][code][cur][volt] │ 32-bit [V][C][P][S]│
  │ 协议号     │ 启发式估算       │ 硬件直接提供 1-10 │
  │ 属性控制   │ MIOT SET/GET    │ FlatBuffers SET  │
  │ 通知通道   │ cmd_recv inline │ 00000005 特征     │
  │ 加密      │ AES-CCM (共享)  │ AES-CCM (共享)   │
  └──────────┴─────────────────┴──────────────────┘

认证/加密共享: BLE Spec 和 MiOT 共用同一 BLE 连接和 AES-CCM 会话密钥.
认证通过 MiOT login 完成后, BLE Spec 数据可发送到 0000001c + 00000005 通道.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List
import struct

from .flatbuffers import encode_set_property, encode_get_property

# ============================================================
# BLE Spec 帧定义 (对应 SDK: uhb.OooO0o0, j1a 帧结构)
# ============================================================

# 帧头常量 (来自 SDK j1a 类, uhb 类)
FRAME_SPEC_SETUP = 0x2005      # 设置帧: [0x05, 0x20] LE, opcode=5, version=2
FRAME_SPEC_RESPONSE = 0x0F20  # 响应帧: [0x0F, 0x20], opcode=0xF20, version=0
FRAME_MIOT = 0x0C20            # MiOT 帧 (保留对照)

SETUP_MARKER = 0xF0           # uhb.OooO0o0() 中最后一个字节

# ============================================================
# 端口数据解析 (对应 SDK: parsePortInfo)
# ============================================================

@dataclass
class PortInfo:
    """BLE Spec 端口数据结构 (32-bit 格式).

    对应米家 App parsePortInfo() 函数.

    Bits:
      31-24: voltage_raw  (V × 10)
      23-16: current_raw  (A × 10)
      15-8:  protocol_num (1-10, 对应 getC1C2ProtocolStr)
      7-0:   port_status  (0=idle, other=active)
    """
    voltage: float = 0.0
    current: float = 0.0
    protocol_number: int = 0   # 米家协议号 1-10
    port_status: int = 0
    active: bool = False

    @classmethod
    def from_spec_value(cls, value: int) -> 'PortInfo':
        """从 32-bit BLE Spec 值解析端口数据."""
        voltage_raw = (value >> 24) & 0xFF
        current_raw = (value >> 16) & 0xFF
        protocol_num = (value >> 8) & 0xFF
        port_status = value & 0xFF

        voltage = voltage_raw / 10.0
        current = current_raw / 10.0
        active = port_status != 0 or voltage > 0 or current > 0

        return cls(
            voltage=round(voltage, 1),
            current=round(current, 1),
            protocol_number=protocol_num,
            port_status=port_status,
            active=active,
        )

    # 米家协议号 → 协议名称映射 (getC1C2ProtocolStr)
    PROTOCOL_MAP: Dict[int, str] = {
        0: "idle",
        1: "5V",
        2: "5V",
        3: "QC",
        4: "AFC",
        5: "FCP",
        6: "SCP",
        7: "PD",
        8: "PPS",
        9: "PPS",
        10: "UFCS",
    }

    @property
    def protocol_name(self) -> str:
        """米家协议名称."""
        return self.PROTOCOL_MAP.get(self.protocol_number, f"Unknown ({self.protocol_number})")

    @property
    def power(self) -> float:
        return round(self.voltage * self.current, 1)

    def to_dict(self) -> dict:
        return {
            "voltage": self.voltage,
            "current": self.current,
            "power": self.power,
            "active": self.active,
            "protocol": self.protocol_name,
            "protocol_number": self.protocol_number,
        }


# ============================================================
# BLE Spec 命令编码 (对应 SDK: m78.setPropertiesValue)
# ============================================================

class BleSpecCommand:
    """构建 BLE Spec 命令.

    对应 SDK 中的:
    - m78.OooO00o(vae) → 构建 FlatBuffers
    - uhb.OooO0o0() → 构建设置帧 [05 20][opcode][F0]
    """

    @staticmethod
    def build_setup_frame() -> bytes:
        """构建设置帧 (uhb.OooO0o0).

        返回: [0x05, 0x20, opcode_lo, opcode_hi, 0xF0]

        对应 SDK:
          ByteBuffer.allocate(5).order(LE)
            .putShort(0x2005).putShort(opcode).put(0xF0)
        """
        return struct.pack('<HHB', FRAME_SPEC_SETUP, 0x0001, SETUP_MARKER)

    @staticmethod
    def encode_set_property(siid: int, piid: int, value: int) -> bytes:
        """编码 SET 属性命令 (FlatBuffers).

        对应 SDK: eee.OooO00o(sae, 4, siid, piid, flag, value, 0, 0, 0, did, 0, 0, 0, 0)
        """
        return encode_set_property(siid=siid, piid=piid, value=value)

    @staticmethod
    def encode_get_property(siid: int, piid: int) -> bytes:
        """编码 GET 属性命令 (FlatBuffers)."""
        return encode_get_property(siid=siid, piid=piid)

    @staticmethod
    def decode_set_response(data: bytes) -> Tuple[bool, Optional[int], Optional[int], Optional[int]]:
        """解码 BLE Spec SET 响应.

        响应格式 (对应 qkb/pkb 类):
          [0x0f 0x20] [seq] [0x00] [type] [count] [siid] [piid] [status] [val_len] [value...]

        Returns:
          (success, siid, piid, value)
        """
        if len(data) < 8:
            return False, None, None, None
        if data[0:2] != b'\x0f\x20':
            return False, None, None, None

        resp_type = data[4]
        siid = data[5] if len(data) > 5 else None
        piid = data[6] if len(data) > 6 else None
        status = data[7] if len(data) > 7 else None

        value = None
        if len(data) >= 13 and data[8] == 4:
            # 4-byte value (0x10 = int type)
            if data[9] in (0x10, 0x14):
                value = int.from_bytes(data[10:14], 'little')
            else:
                value = data[9]

        success = status == 0
        return success, siid, piid, value


# ============================================================
# BLE Spec 通知处理 (对应 SDK: BLESpecNotifyActionEvent)
# ============================================================

@dataclass
class SpecNotification:
    """BLE Spec 通知数据.

    对应 SDK: BLESpecNotifyActionEvent 触发时收到的数据.
    """
    piid: int          # 属性 ID
    value_raw: int     # 原始值
    timestamp: int = 0  # 时间戳 (如果有)

    @classmethod
    def parse_ports(cls, notifications: List['SpecNotification']) -> Dict[int, PortInfo]:
        """从通知列表中解析端口数据.

        PIID 1-4 对应 C1/C2/C3/A 端口.
        """
        ports = {}
        for notif in notifications:
            if 1 <= notif.piid <= 4:
                ports[notif.piid] = PortInfo.from_spec_value(notif.value_raw)
        return ports


# ============================================================
# 协议号映射 (对齐米家 getC1C2ProtocolStr)
# ============================================================

MIJIA_PROTOCOL_NAMES: Dict[int, str] = {
    0: "idle",
    1: "5V",
    2: "5V",
    3: "QC",
    4: "AFC",
    5: "FCP",
    6: "SCP",
    7: "PD",
    8: "PPS",
    9: "PPS",
    10: "UFCS",
}


def get_mijia_protocol_name(proto_num: int) -> str:
    """获取米家协议名称 (对应 SDK getC1C2ProtocolStr)."""
    return MIJIA_PROTOCOL_NAMES.get(proto_num, f"Unknown ({proto_num})")


# ============================================================
# PIID 21 协议开关编码 (对应 SDK parseProtocolExtend / setProtocolExtend)
# ============================================================

PROTOCOL_SWITCH_BITS: Dict[str, Dict[str, int]] = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2, "_reserved": 3},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10, "_reserved": 11},
    "c3": {"ufcs": 16, "scp": 17},
    "a":  {"ufcs": 24, "scp": 25},
}


def encode_protocol_extend(switches: dict) -> int:
    """编码 PIID 21 值 (对齐米家 setProtocolExtend).

    Args:
        switches: {port: {protocol: bool}}
    """
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


def decode_protocol_extend(value: int) -> Dict[str, Dict[str, bool]]:
    """解码 PIID 21 值 (对齐米家 parseProtocolExtend).

    Args:
        value: PIID 21 的 32-bit 值

    Returns:
        {port: {protocol: bool}}
    """
    return {
        "c1": {
            "pd": bool(value & (1 << 0)),
            "pps": bool(value & (1 << 1)),
            "ufcs": bool(value & (1 << 2)),
        },
        "c2": {
            "pd": bool(value & (1 << 8)),
            "pps": bool(value & (1 << 9)),
            "ufcs": bool(value & (1 << 10)),
        },
        "c3": {
            "scp": bool(value & (1 << 17)),
            "ufcs": bool(value & (1 << 16)),
        },
        "a": {
            "scp": bool(value & (1 << 25)),
            "ufcs": bool(value & (1 << 24)),
        },
    }
