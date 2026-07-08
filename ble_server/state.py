"""CUKTECH BLE Server - State management."""
import asyncio
from dataclasses import dataclass
from typing import Dict

PORT_DEFAULT = {"voltage": 0.0, "current": 0.0, "power": 0.0, "active": False, "protocol": "idle"}
PORT_NAMES = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}

VALID_PIIDS = {5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 19, 20}
PIID_RANGES = {
    5: (1, 4), 6: (0, 5), 8: (0, 1440), 9: (0, 1440), 10: (0, 1440),
    11: (0, 1440), 12: (0, 1440), 13: (0, 1), 15: (0, 1), 16: (0, 15),
    19: (0, 1), 20: (0, 1),
}

PROTOCOL_NAMES = {
    0x01: "PD", 0x03: "PD", 0x04: "PD", 0x05: "PD", 0x06: "PD",
    0x07: "PD Fixed", 0x08: "PD PPS", 0x0a: "PD", 0x0b: "PD",
    0x30: "PD", 0x60: "USB-A", 0x70: "QC", 0x80: "PD",
}

PD_FIXED_VOLTAGES = {5.0, 9.0, 12.0, 15.0, 20.0}
QC_VOLTAGES = {5.0, 9.0, 12.0, 15.0, 20.0}
PDO_KIND_BY_HIGH_BYTE = {
    0x07: "PD Fixed",
    0x08: "PD PPS",
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

    def __eq__(self, other):
        if not isinstance(other, PortState):
            return False
        return (self.voltage == other.voltage and self.current == other.current
                and self.power == other.power and self.active == other.active
                and self.protocol == other.protocol)


class ChargerState:
    def __init__(self):
        self.connected: bool = False
        self.authenticated: bool = False
        self.ports: Dict[int, PortState] = {i: PortState() for i in range(1, 5)}
        self.settings: Dict[str, int] = {}
        self.pdo_caps: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._cache: dict = {}
        self._cache_valid = False

    def _invalidate_cache(self):
        self._cache_valid = False

    async def update_port(self, piid: int, data: dict):
        async with self._lock:
            self.ports[piid] = PortState(**data)
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

    async def to_dict(self):
        async with self._lock:
            if self._cache_valid:
                return self._cache
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
            }
            self._cache_valid = True
            return self._cache


def decode_port(piid, pt, pdo_data=None):
    if len(pt) < 12:
        return None
    b = pt[-4:]
    in_use = b[0]
    protocol_code = b[1]
    current = b[2] / 10.0
    voltage = b[3] / 10.0
    active = bool(in_use) or (voltage > 0) or (current > 0)
    if in_use:
        protocol = PROTOCOL_NAMES.get(protocol_code, f"Unknown (0x{protocol_code:02X})")
        # 如果 PDO 能力字有类型信息，使用它
        if pdo_data and protocol_code in (0x0a, 0x07):
            pdo_kind = pdo_data.get("kind")
            if pdo_kind:
                protocol = pdo_kind
            # 否则用电压范围区分 PD Fixed 和 PPS
            elif protocol in ("PD Fixed", "PD") and active:
                # 检查电压是否是标准 PD Fixed 电压
                is_standard_voltage = any(abs(voltage - v) < 0.5 for v in PD_FIXED_VOLTAGES)
                if not is_standard_voltage and voltage > 3.0:
                    protocol = "PD PPS"
                elif protocol == "PD":
                    protocol = "PD Fixed"
        # USB-A/QC 电压范围检测
        elif protocol_code == 0x60 and active:
            # 0x60 包括 DCP 和 QC，用电压区分
            if voltage > 5.5:  # 超过 5V 基本是 QC
                protocol = "QC"
        # C3 端口族代码 (0x80) 区分 PD 和 QC
        elif protocol_code == 0x80 and active:
            # 0x80 覆盖 5-15V PD/PPS，但根据电压判断
            if voltage < 9.5:  # 低于 9.5V 可能是 QC
                protocol = "QC"
            else:  # 9.5V 以上是 PD
                protocol = "PD"
    else:
        protocol = "idle"
    return {
        "voltage": round(voltage, 1),
        "current": round(current, 1),
        "power": round(voltage * current, 1),
        "active": active,
        "protocol": protocol,
    }


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
