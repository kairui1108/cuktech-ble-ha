"""PIID 21 协议开关位编码的纯函数编解码。

与 Home Assistant 集成实体解耦：只接收/返回纯数据，
便于独立单测，也让协调器从位运算细节中解脱。
"""
from __future__ import annotations

from .const import PROTOCOL_BITS


def decode_protocol_switches(value: int | None) -> dict[str, dict[str, bool]]:
    """解码 PIID 21 位编码为 {'c1': {'pd': bool, ...}, ...} 结构。"""
    v = value if value is not None else 0
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    result: dict[str, dict[str, bool]] = {}
    for port, protos in PROTOCOL_BITS.items():
        result[port] = {
            proto: bool(v & (1 << bit)) for proto, bit in protos.items()
        }
    return result


def _c1c2_flags(ps: dict | None) -> int:
    """编码 C1/C2 端口标志 (PD/PPS/UFCS + 保留位固定为 1)。"""
    if not ps:
        return 0
    v = 0x08  # 保留位固定为 1
    if ps.get("pd"):
        v |= 0x01
    if ps.get("pps"):
        v |= 0x02
    if ps.get("ufcs"):
        v |= 0x04
    return v


def _c3a_flags(ps: dict | None) -> int:
    """编码 C3/A 端口标志 (UFCS/SCP)。"""
    if not ps:
        return 0
    v = 0
    if ps.get("ufcs"):
        v |= 0x01
    if ps.get("scp"):
        v |= 0x02
    return v


def encode_protocol_switches(switches: dict) -> int:
    """把协议开关字典编码回 PIID 21 位值。

    Args:
        switches: {'c1': {'pd','pps','ufcs': bool}, 'c2': ..., 'c3': ..., 'a': ...}
    Returns:
        PIID 21 的 int 值。
    """
    c1 = _c1c2_flags(switches.get("c1"))
    c2 = _c1c2_flags(switches.get("c2"))
    c3 = _c3a_flags(switches.get("c3"))
    a = _c3a_flags(switches.get("a"))
    return (a << 24) | (c3 << 16) | (c2 << 8) | c1