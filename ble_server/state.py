"""CUKTECH BLE Server - State management."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict

from src.cuktech_ble.protocol import PDO_KIND_BY_HIGH_BYTE

_LOGGER = logging.getLogger(__name__)

# 协议检测引擎 (V2)
from state_protocol_v2 import decode_port as _decode_port_v2

PORT_DEFAULT = {"voltage": 0.0, "current": 0.0, "power": 0.0, "active": False, "protocol": "idle"}
PORT_NAMES = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}

# PIIDs that can be set via commands (read/write)
# PIID 1-4: port data (read-only, pushed by device)
# PIID 7: protocol control (write-only, not included)
# PIID 14: screen direction (write-only, not included)
# PIID 17-18: PDO capabilities (read-only, fetched separately)
# PIID 21: protocol extend control (read/write)
VALID_PIIDS = {5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 19, 20, 21}
PIID_RANGES = {
    5: (1, 4), 6: (0, 5), 8: (0, 1440), 9: (0, 1440), 10: (0, 1440),
    11: (0, 1440), 12: (0, 1440), 13: (0, 1), 15: (0, 1), 16: (0, 15),
    19: (0, 1), 20: (0, 1),
    21: (0, 0xFFFFFFFF),  # 协议扩展控制 32-bit
}

# 米家协议开关位定义 (PIID 21)
# c1Flags: bit0=PD, bit1=PPS, bit2=UFCS, bit3=保留(固定1)
# c2Flags: 同上
# c3Flags: bit0=UFCS, bit1=SCP
# aFlags:  bit0=UFCS, bit1=SCP
# 编码: aFlags<<24 | c3Flags<<16 | c2Flags<<8 | c1Flags
PROTOCOL_SWITCH_BITS = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2, "_reserved": 3},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10, "_reserved": 11},
    "c3": {"ufcs": 16, "scp": 17},
    "a":  {"ufcs": 24, "scp": 25},
}


@dataclass
class PortState:
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    active: bool = False
    protocol: str = "idle"

    def to_dict(self):
        return {
            "voltage": self.voltage,
            "current": self.current,
            "power": self.power,
            "active": self.active,
            "protocol": self.protocol,
        }


class ChargerState:
    def __init__(self):
        self.connected: bool = False
        self.authenticated: bool = False
        self.ports: Dict[int, PortState] = {i: PortState() for i in range(1, 5)}
        self.settings: Dict[str, int] = {}
        self.pdo_caps: Dict[str, dict] = {}
        self.device_model: str = ""
        self.firmware_version: str = ""
        self._lock = asyncio.Lock()
        self._cache: dict = {}
        self._cache_valid = False
        # PIID 21 协议扩展控制值
        self._protocol_extend: int = 0
        # 用户期望的协议开关状态 (不受设备跨口联动污染, 用于编码 SET)
        self._desired_switches: dict = {}

    @property
    def protocol_extend(self) -> int:
        """PIID 21 原始值 (设备回读)."""
        return self._protocol_extend

    @property
    def protocol_switches(self) -> dict:
        """解析后的协议开关状态（对齐米家 parseProtocolExtend）."""
        v = self._protocol_extend
        return {
            "c1": {
                "pd": bool(v & (1 << 0)),
                "pps": bool(v & (1 << 1)),
                "ufcs": bool(v & (1 << 2)),
            },
            "c2": {
                "pd": bool(v & (1 << 8)),
                "pps": bool(v & (1 << 9)),
                "ufcs": bool(v & (1 << 10)),
            },
            "c3": {
                "scp": bool(v & (1 << 17)),
                "ufcs": bool(v & (1 << 16)),
            },
            "a": {
                "scp": bool(v & (1 << 25)),
                "ufcs": bool(v & (1 << 24)),
            },
        }

    @property
    def desired_switches(self) -> dict:
        """用户期望的协议开关状态 (首次从设备同步, 后续由用户操作更新).

        与 protocol_switches 的区别:
          - protocol_switches: 设备回读的真实状态 (会被跨口联动清零)
          - desired_switches: 用户期望的状态 (用于编码 SET, 不受设备副作用污染)
        """
        if not self._desired_switches:
            # 首次初始化: 从设备当前状态同步
            self._desired_switches = dict(self.protocol_switches)
        return self._desired_switches

    def update_desired_switch(self, port: str, proto: str, on: bool):
        """更新用户期望的协议开关."""
        switches = self.desired_switches  # 触发首次同步
        if port not in switches:
            switches[port] = {}
        switches[port][proto] = on

    @staticmethod
    def encode_protocol_extend(switches: dict) -> int:
        """根据开关状态编码 PIID 21 值（对齐米家 setProtocolExtend）.

        硬件约束:
          - PPS 依赖 PD: PD 关闭 → PPS 不可用 (PD 3.0 特性)
          - UFCS 依赖 PD: PD 关闭 → UFCS 不可用 (该端口无独立 Bypass 通路)
          - C3/A 口: UFCS 依赖 PD?
        """
        def _c1c2_flags(ps):
            if not ps:
                return 0
            v = 0x08  # 保留位固定为 1
            if ps.get("pd"):
                v |= 0x01
                if ps.get("pps"):  v |= 0x02
                if ps.get("ufcs"): v |= 0x04
            # PD 关闭 → 强制清零 PPS / UFCS
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

    def _invalidate_cache(self):
        self._cache_valid = False

    async def update_port(self, piid: int, data: dict):
        async with self._lock:
            # 过滤掉 V2 内部调试字段（以 _ 开头），只保留 PortState 需要的字段
            clean_data = {k: v for k, v in data.items() if not k.startswith('_')}
            self.ports[piid] = PortState(**clean_data)
            self._invalidate_cache()

    async def update_settings(self, settings: dict):
        async with self._lock:
            self.settings.update(settings)
            self._invalidate_cache()

    async def set_connection(self, connected: bool, authenticated: bool):
        async with self._lock:
            self.connected = connected
            self.authenticated = authenticated
            self._invalidate_cache()

    async def update_pdo_caps(self, pdo_caps: dict):
        async with self._lock:
            self.pdo_caps = pdo_caps
            self._invalidate_cache()

    async def update_protocol_extend(self, value: int):
        """Update PIID 21 protocol extend value."""
        async with self._lock:
            self._protocol_extend = value
            self.settings["21"] = value
            self._invalidate_cache()

    async def update_device_info(self, model: str, firmware: str):
        """Update device model and firmware version from BLE."""
        async with self._lock:
            self.device_model = model
            self.firmware_version = firmware
            self._invalidate_cache()

    async def to_dict(self):
        async with self._lock:
            if self._cache_valid:
                return dict(self._cache)
            port_ctl = self.settings.get("16", 0x0F)
            port_enabled = {
                1: bool(port_ctl & (1 << 0)),
                2: bool(port_ctl & (1 << 1)),
                3: bool(port_ctl & (1 << 2)),
                4: bool(port_ctl & (1 << 3)),
            }
            self._cache = {
                "connected": self.connected,
                "authenticated": self.authenticated,
                "ports": {k: {**v.to_dict(), "enabled": port_enabled.get(k, False)} for k, v in self.ports.items()},
                "settings": dict(self.settings),
                "protocol_extend": self._protocol_extend,
                "protocol_switches": self.protocol_switches,
                "device_model": self.device_model,
                "firmware_version": self.firmware_version,
            }
            self._cache_valid = True
            return self._cache


def decode_port(piid, pt, pdo_data=None, protocol_switches=None):
    """解码端口数据，使用 V2 协议检测引擎.

    Args:
        piid: 端口 ID (1-4)
        pt: 解密后的 MiOT 属性负载 (bytes)
        pdo_data: PDO 能力信息 (可选, PIID 17/18)
        protocol_switches: PIID 21 当前协议开关状态 (可选)

    Returns:
        端口数据字典: {voltage, current, power, active, protocol, ...}
        或 None (数据无效)
    """
    return _decode_port_v2(piid, pt, pdo_data, protocol_switches=protocol_switches)


def decode_pdo_caps(value, high_port, low_port):
    low_half = value & 0xFFFF
    high_half = (value >> 16) & 0xFFFF
    def _cap(half):
        byte = half & 0xFF
        return byte or None
    def _kind(half):
        if (half & 0xFF) == 0:
            return None
        return PDO_KIND_BY_HIGH_BYTE.get((half >> 8) & 0xFF)
    return {
        low_port: {"cap": _cap(low_half), "kind": _kind(low_half)},
        high_port: {"cap": _cap(high_half), "kind": _kind(high_half)},
    }
