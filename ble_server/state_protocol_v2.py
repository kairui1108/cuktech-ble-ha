"""
充电协议检测模块 - 对齐米家App

米家 App 酷态科插件协议映射表:
  protocol 1/2 → "5V"    普通充电
  protocol 3   → "QC"    Quick Charge
  protocol 4   → "AFC"   Samsung AFC
  protocol 5   → "FCP"   Huawei FCP
  protocol 6   → "SCP"   Huawei SCP
  protocol 7   → "PD"    USB-PD Fixed
  protocol 8/9 → "PPS"   PD PPS
  protocol 10  → "UFCS"  融合快充

注意: MiOT 模式下无法直接获取硬件协议号 (1-10)。
本模块通过电压、原始 code 字节、端口类型进行启发式估算。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, List


# ============================================================
# 米家协议映射表 (与米家App完全一致)
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
# 标准电压档位 (单位: V)
# ============================================================
PD_FIXED_VOLTAGES = [5.0, 9.0, 12.0, 15.0, 20.0]
QC_VOLTAGES = [5.0, 9.0, 12.0, 20.0]
PPS_VOLTAGE_RANGE = (3.0, 21.0)  # PPS 电压范围


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
    code: int           # 原始 code 字节 (非米家协议号)
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
            code=b[1],
            current_raw=b[2],
            voltage_raw=b[3],
        )


# ============================================================
# 协议号估算 (核心逻辑)
# ============================================================

def _calc_voltage_match_score(voltage: float, refs: List[float], tolerance: float = 0.5) -> float:
    """计算电压与参考档位的匹配度 (0.0 ~ 1.0)."""
    if not refs:
        return 0.0
    min_dist = min(abs(voltage - r) for r in refs)
    if min_dist >= tolerance:
        return 0.0
    return 1.0 - min_dist / tolerance


def _estimate_pd_subtype(voltage: float, code: int) -> int:
    """
    估算 C1/C2 端口的 PD 子类型.

    设备固件的 code byte 不严格区分 PD Fixed vs PPS (如 code=0x04 既可能
    是 PD 也可能是 PPS)。通过电压与 PD 标准档位的距离来判定:
      - 低压段 (<12V): PPS 非常常见，仅极精准匹配 PD 档位才判 PD
      - 高压段 (≥12V): PD 常见，宽松匹配 PD 档位即判 PD

    Args:
        voltage: 实际电压
        code: 原始 code 字节

    Returns:
        米家协议号: 7=PD, 8=PPS
    """
    min_dist = min(abs(voltage - v) for v in PD_FIXED_VOLTAGES)

    if voltage < 12.0:
        # 低压段: PPS 极常见 (3-12V 全程覆盖)
        if round(min_dist, 4) <= 0.05:
            return 7  # 极精准匹配 PD 标准档位 → PD
        return 8      # 默认 PPS

    # 高压段 (≥12V): PD 更常见 (PPS 极少超过 15V)
    if round(min_dist, 4) <= 0.3:
        return 7      # PD
    if PPS_VOLTAGE_RANGE[0] <= voltage <= PPS_VOLTAGE_RANGE[1]:
        return 8      # PPS
    return 7          # PD


def estimate_protocol_number(piid: int, raw: RawPortData, pdo_data: Optional[Dict] = None,
                              protocol_switches: Optional[Dict] = None) -> int:
    """
    估算米家协议号 (1-10).

    对齐米家 App 逻辑:
      - PIID 17/18 PDO 能力数据提供端口是否支持 PPS
      - PIID 21 protocol_switches 提供当前是否已启用 PD
      - 结合电压判断当前是 PD Fixed、PPS 还是 5V

    硬件约束 (AD1204U):
      - PPS 依赖 PD 3.0: PD 关闭时 PPS/UFCS 也被设备关闭
      - 端口协议联动: C1 协议变更会触发全端口功率重协商

    Returns:
        米家协议号，0 表示空闲/无法确定
    """
    voltage = raw.voltage
    code = raw.code

    if piid in (1, 2):
        # ===== C1/C2: Type-C 全系列 PD =====

        # PD 关闭时端口只能输出 5V (无法进入 PD 协商)
        if protocol_switches:
            port_key = {1: "c1", 2: "c2"}.get(piid)
            sw = protocol_switches.get(port_key, {})
            if not sw.get("pd", True) and voltage > 0:
                return 1  # 5V (PD 关闭，协议失效)

        if code == 0x08:       return 8   # PPS 明确标识
        if code == 0x70:
            match_score = _calc_voltage_match_score(voltage, PD_FIXED_VOLTAGES)
            if match_score > 0.9:
                return 7  # PD
            return 3      # QC

        # PD 系列 code: 接入 PDO 数据对齐米家判断逻辑
        # 注意: PDO 数据可能是动态的 (充电状态变化时 PIID17 值会变)
        if code in (0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0A, 0x0B, 0x30):
            pdo_kind = pdo_data.get("kind") if pdo_data else None
            pps_enabled = (protocol_switches or {}).get({1:"c1",2:"c2"}[piid], {}).get("pps", True)
            if pdo_kind == "PD PPS":
                min_dist = min(abs(voltage - v) for v in PD_FIXED_VOLTAGES)
                if round(min_dist, 4) <= 0.05:
                    return 7  # 极精准匹配 PD 档位
                return 8      # 默认 PPS
            elif pdo_kind == "PD Fixed":
                # PDO 说 PD Fixed 但 PIID21 中 PPS 是开的 → PDO 可能是动态值
                if pps_enabled and voltage < 12.0:
                    return _estimate_pd_subtype(voltage, code)  # 电压启发式
                return 7
            # 无 PDO 数据 → 电压启发式兜底
            return _estimate_pd_subtype(voltage, code)

        # 其他 code: 电压法兜底
        match_score = _calc_voltage_match_score(voltage, PD_FIXED_VOLTAGES)
        if match_score > 0.7:  return 7
        if PPS_VOLTAGE_RANGE[0] <= voltage <= PPS_VOLTAGE_RANGE[1]: return 8
        return 0
    
    elif piid == 3:
        # ===== C3: 混合口 =====
        if code == 0x70:       return 3   # QC 明确标识
        
        # 电压优先策略 (对齐米家实测结果)
        if voltage >= 15.0:    return 7   # 15V+ → PD
        if voltage >= 8.5:     return 3   # 8.5-15V → C3 倾向 QC
        if voltage <= 5.5:     return 1   # 5.5V- → 5V
        # 过渡区 (5.5-8.5V)
        return 3 if voltage > 6.0 else 1
    
    elif piid == 4:
        # ===== USB-A =====
        if code == 0x70:       return 3   # QC 明确标识
        if voltage > 5.5:      return 3   # QC
        if voltage > 0:        return 1   # 5V
    
    return 0


# ============================================================
# 主入口函数
# ============================================================

def decode_port_v2(
    piid: int,
    payload: bytes,
    pdo_data: Optional[Dict] = None,
    thresholds=None,  # 保留参数兼容，不再使用
    protocol_switches: Optional[Dict] = None,
) -> Optional[Dict]:
    """解码端口数据 (V2).

    Args:
        piid: 端口 ID (1-4)
        payload: 解密后的 MiOT 属性负载
        pdo_data: PDO 能力信息 (PIID 17/18)
        protocol_switches: PIID 21 当前协议开关状态

    Returns:
        端口数据字典，或 None
    """
    raw = RawPortData.from_payload(payload)
    if raw is None:
        return None

    is_active = raw.in_use or raw.voltage > 0 or raw.current > 0

    if not is_active:
        protocol = "idle"
        confidence = 1.0
        method = "no_load"
        proto_num = 0
    else:
        proto_num = estimate_protocol_number(piid, raw, pdo_data, protocol_switches)
        protocol = get_mijia_protocol_name(proto_num)
        confidence = 0.90 if proto_num > 0 else 0.30
        method = f"proto_{proto_num}"

    # 构造 BLE Spec 32-bit 格式值 (对齐米家 parsePortInfo)
    # [voltage_raw:8][current_raw:8][protocol_num:8][status:8]
    voltage_raw = min(int(round(raw.voltage, 1) * 10), 255)
    current_raw = min(int(round(raw.current, 1) * 10), 255)
    status = 1 if is_active else 0
    blespec32 = (voltage_raw << 24) | (current_raw << 16) | (proto_num << 8) | status

    return {
        "voltage": round(raw.voltage, 1),
        "current": round(raw.current, 1),
        "power": raw.power,
        "active": is_active,
        "protocol": protocol,
        "_confidence": confidence,
        "_detection_method": method,
        "_raw_code": f"0x{raw.code:02X}",
        "_proto_num": proto_num if is_active else None,
        "_blespec32": blespec32,  # BLE Spec 32-bit 格式值
    }


# ============================================================
# 向后兼容包装器
# ============================================================
def decode_port(piid, pt, pdo_data=None, protocol_switches=None):
    """向后兼容接口."""
    return decode_port_v2(piid, pt, pdo_data, protocol_switches=protocol_switches)
