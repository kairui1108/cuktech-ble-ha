"""
充电协议检测模块 - 对齐米家App + 固件逆向验证 (v3)

权威数据源: PIID 17/18 (c1_c2_protocol / c3_a_protocol)
  固件在 PD/PPS 协商变更时主动推送, 由 ble_manager._handle_hw_protocol_push
  即时写入 state 缓存。协议号为米家编制: 7=PD 8/9=PPS 10=UFCS 3=QC ...

历史版本(v2)曾用推送帧的 code 字节做启发式推断。固件反汇编证实该字节是
PDO 档位索引/硬件状态码而非协议号(20V PD 充电中在 0x0B/0x04 跳变),
基于它的规则属于相关性巧合, 已在 v3 移除。

保留的最小兜底(仅电压规则): PIID17 从未到达且 GET 失败的冷启动场景。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


# ============================================================
# 米家协议映射表 
# ============================================================
MIJIA_PROTOCOLS: Dict[int, str] = {
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
    """根据米家协议号返回协议名称."""
    return MIJIA_PROTOCOLS.get(proto_num, f"Unknown (0x{proto_num:02X})")


# ============================================================
# 端口类型
# ============================================================
class PortType(Enum):
    TYPE_C_12 = "type_c_12"   # C1/C2: Type-C, 支持全系列 PD
    TYPE_C_3 = "type_c_3"     # C3: 混合口, 支持 PD + QC
    USB_A = "usb_a"           # A口: USB-A + QC


def get_port_type(piid: int) -> PortType:
    if piid in (1, 2):
        return PortType.TYPE_C_12
    elif piid == 3:
        return PortType.TYPE_C_3
    elif piid == 4:
        return PortType.USB_A
    raise ValueError(f"Invalid PIID: {piid}")


# ============================================================
# 原始数据
# ============================================================
@dataclass
class RawPortData:
    """从 BLE 解密后的原始端口数据."""
    in_use: bool
    status_raw: int     # 原始 status 字节 (0x00=空闲, 0x01=单口, 0x11=C3+A合并)
    code: int           # 原始 code 字节 (PDO 档位索引/硬件状态码 — 非协议号!)
    current_raw: int    # 原始电流 (×10 mA)
    voltage_raw: int    # 原始电压 (×10 mV)

    @property
    def current(self) -> float:
        return self.current_raw / 10.0

    @property
    def voltage(self) -> float:
        return self.voltage_raw / 10.0

    @property
    def power(self) -> float:
        return round(self.voltage * self.current, 1)

    @classmethod
    def from_payload(cls, payload: bytes) -> Optional['RawPortData']:
        """从 MiOT 属性负载解析."""
        if len(payload) < 12:
            return None
        b = payload[-4:]
        return cls(
            in_use=bool(b[0]),
            status_raw=b[0],
            code=b[1],
            current_raw=b[2],
            voltage_raw=b[3],
        )


# ============================================================
# 最小兜底 (仅当 PIID17/18 从未到达时使用; 正常路径不进入)
# ============================================================
PD_FIXED_VOLTAGES = [5.0, 9.0, 12.0, 15.0, 20.0]


def _fallback_by_voltage(piid: int, voltage: float) -> int:
    """纯电压兜底: 仅在 hw_protocol 缺失时使用.

    规则基于 USB-PD 物理规范（非 code 字节相关性）:
      - PD Fixed 档位固定为 5/9/12/15/20V
      - PPS 特征是 3.3-21V 连续可调 → 电压偏离所有固定档的高压 C 口输出必为 PPS
    粗粒度: 不区分 AFC/FCP/SCP (无硬件依据的猜测比粗粒度更糟)。
    """
    if piid == 4:
        return 3 if voltage > 5.5 else 1
    if piid == 3:
        if voltage >= 15.0:
            return 7
        return 3 if voltage >= 8.5 else 1
    # C1/C2
    if voltage <= 5.5:
        return 1
    if any(abs(voltage - v) < 0.25 for v in PD_FIXED_VOLTAGES):
        return 7   # 精准落在 PD 固定档 → PD Fixed
    return 8       # 非档位连续电压 → PPS


def estimate_protocol_number(piid: int, raw: 'RawPortData', pdo_data=None,
                             protocol_switches=None,
                             hw_protocol: Optional[int] = None) -> int:
    """协议号判定入口.

    优先级:
      1. hw_protocol ∈ 1-10 (PIID17/18 推送或 GET, 米家编制) — 权威
      2. hw_protocol 为 MiOT code (>10, C3/A 口特例) → 映射回米家编号
      3. 电压兜底 (仅 hw_protocol 缺失时; 粗粒度)
    """
    if hw_protocol is not None and hw_protocol > 0:
        if hw_protocol <= 10:
            return hw_protocol
        # C3/A 口的 MiOT code (0x60=USB-A, 0x70=QC, 0x80=PD) → 米家编号
        mapped = {0x70: 3, 0x80: 7}.get(hw_protocol)
        if mapped:
            return mapped

    return _fallback_by_voltage(piid, raw.voltage)


# ============================================================
# 主入口函数
# ============================================================

def decode_port_v2(
    piid: int,
    payload: bytes,
    pdo_data=None,
    thresholds=None,
    protocol_switches=None,
    hw_protocol: Optional[int] = None,
) -> Optional[Dict]:
    """解码端口数据 (v3).

    Args:
        piid: 端口 ID (1-4)
        payload: 解密后的 MiOT 属性负载
        pdo_data: 兼容参数 (v3 不再使用 — kind 字节语义已由固件证伪)
        protocol_switches: PIID 21 协议开关 (兼容参数)
        hw_protocol: PIID17/18 权威协议号 (优先)

    Returns:
        端口数据字典，或 None
    """
    raw = RawPortData.from_payload(payload)
    if raw is None:
        return None

    is_active = raw.in_use or raw.voltage > 0 or raw.current > 0

    if not is_active:
        protocol = "idle"
        method = "no_load"
    else:
        proto_num = estimate_protocol_number(
            piid, raw, pdo_data, protocol_switches, hw_protocol)
        protocol = get_mijia_protocol_name(proto_num)
        method = f"proto_{proto_num}"

    return {
        "voltage": round(raw.voltage, 1),
        "current": round(raw.current, 1),
        "power": raw.power,
        "active": is_active,
        "status_raw": raw.status_raw,
        "protocol": protocol,
        "_raw_code": f"0x{raw.code:02X}",
        "_proto_num": proto_num if is_active else None,
    }


# ============================================================
# 向后兼容包装器
# ============================================================
def decode_port(piid, pt, pdo_data=None, protocol_switches=None, hw_protocol=None):
    """向后兼容接口."""
    return decode_port_v2(piid, pt, pdo_data,
                          protocol_switches=protocol_switches,
                          hw_protocol=hw_protocol)
