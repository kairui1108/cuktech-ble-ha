"""
协议检测模块测试 - v3 (hw_protocol 权威 + 电压兜底)

对齐固件逆向结论:
  - 推送帧 code 字节是 PDO 档位索引, 不参与协议判断
  - PIID17/18 协议号(米家 1-10 编制)为唯一权威源
  - 电压兜底仅在 hw_protocol 缺失时使用, 粗粒度(PD/QC/5V)
"""

import sys
from pathlib import Path

_ble_server_dir = Path(__file__).parent.parent
sys.path.insert(0, str(_ble_server_dir))

from state_protocol_v2 import (
    decode_port_v2,
    decode_port,
    RawPortData,
    estimate_protocol_number,
    get_mijia_protocol_name,
    MIJIA_PROTOCOLS,
    get_port_type,
    PortType,
)


def make_test_payload(in_use: int, code: int, current: float, voltage: float) -> bytes:
    """创建测试 payload: [header:8B][in_use][code][current_raw][voltage_raw]"""
    header = bytes(8)
    return header + bytes([in_use, code, int(current * 10), int(voltage * 10)])


def test_raw_port_data():
    """测试原始数据解析."""
    raw = RawPortData.from_payload(make_test_payload(1, 0x07, 2.0, 9.0))
    assert raw.voltage == 9.0
    assert raw.current == 2.0
    assert raw.code == 0x07

    assert RawPortData.from_payload(bytes(5)) is None


def test_mijia_protocol_names():
    """验证协议名称与米家完全一致."""
    expected = {
        0: "idle", 1: "5V", 2: "5V", 3: "QC",
        4: "AFC", 5: "FCP", 6: "SCP",
        7: "PD", 8: "PPS", 9: "PPS", 10: "UFCS",
    }
    for num, name in expected.items():
        actual = get_mijia_protocol_name(num)
        assert actual == name, f"协议{num}: 期望'{name}', 得到'{actual}'"
    assert get_mijia_protocol_name(99) == "Unknown (0x63)"


def test_hw_protocol_authoritative():
    """权威源测试: hw_protocol 直接决定结果, code 字节不影响."""
    cases = [
        # (name, piid, code, volt, hw_proto, expected_proto)
        ("C2 hw=7(PD) 20V code跳变",   2, 0x0B, 20.0, 7, 7),
        ("C2 hw=7(PD) code=0x04",      2, 0x04, 20.0, 7, 7),
        ("C1 hw=9(PPS) 5.1V",          1, 0x07, 5.1, 9, 9),
        ("C1 hw=8(PPS) 任意code",      1, 0x33, 8.4, 8, 8),
        ("C3 hw=3(QC) 12V",            3, 0x70, 12.1, 3, 3),
        ("A  hw=6(SCP)",               4, 0x60, 4.95, 6, 6),
        ("UFCS hw=10",                 1, 0x07, 5.0, 10, 10),
    ]
    for name, piid, code, volt, hw, expect in cases:
        p = make_test_payload(1, code, 1.0, volt)
        raw = RawPortData.from_payload(p)
        got = estimate_protocol_number(piid, raw, hw_protocol=hw)
        assert got == expect, f"{name}: 期望{expect}, 得到{got}"


def test_miot_code_mapping():
    """C3/A 口 hw_protocol 为 MiOT code (>10) 时映射回米家编号."""
    p = make_test_payload(1, 0x00, 1.0, 9.0)
    raw = RawPortData.from_payload(p)
    # 0x70 (QC code) → 3(QC), 0x80 → 7(PD)
    assert estimate_protocol_number(3, raw, hw_protocol=0x70) == 3
    assert estimate_protocol_number(3, raw, hw_protocol=0x80) == 7
    # 无法映射的 code (>10 且非 0x70/0x80) → 退电压兜底
    assert estimate_protocol_number(3, raw, hw_protocol=0x55) == 3  # 9V → QC


def test_fallback_voltage_only():
    """电压兜底(仅 hw_protocol 缺失时): 粗粒度 PD/QC/5V."""
    cases = [
        # (piid, volt, expected) — code 不再影响结果
        (1, 20.0, 7),   # C1 高压 PD 档位
        (1, 9.0, 7),    # C1 9V (PD 档位)
        (1, 5.0, 1),    # C1 5V
        (2, 12.0, 7),
        (3, 20.0, 7),   # C3 高压 → PD
        (3, 12.0, 3),   # C3 中压 → QC
        (3, 5.0, 1),
        (4, 9.0, 3),    # A口 >5V → QC
        (4, 5.0, 1),
    ]
    for piid, volt, expect in cases:
        p = make_test_payload(1, 0x00, 1.0, volt)  # code=0 — 无任何提示
        raw = RawPortData.from_payload(p)
        got = estimate_protocol_number(piid, raw)
        assert got == expect, f"兜底 piid={piid} V={volt}: 期望{expect}, 得到{got}"


def test_decode_port_v2():
    """完整解码: hw_protocol 主路径."""
    cases = [
        # (name, piid, code, cur, volt, hw_proto, expected_name)
        ("C2 PD 20V",       2, 0x0B, 1.5, 20.1, 7, "PD"),
        ("C1 PPS 9.2V",     1, 0x07, 1.2, 9.2, 9, "PPS"),
        ("C3 QC 12V",       3, 0x60, 0.3, 12.1, None, "QC"),
        ("USB-A 5V",        4, 0x60, 1.0, 5.0, None, "5V"),
        ("idle",            1, 0x00, 0.0, 0.0, None, "idle"),
    ]
    for name, piid, code, cur, volt, hw, expected in cases:
        p = make_test_payload(1 if volt > 0 else 0, code, cur, volt)
        r = decode_port_v2(piid, p, hw_protocol=hw)
        assert r["protocol"] == expected, f"{name}: 期望'{expected}', 得到'{r['protocol']}'"
        assert "_confidence" not in r  # v3 已移除置信度伪指标
        assert r["voltage"] == round(volt, 1)


def test_decode_port_compat():
    """向后兼容包装器."""
    p = make_test_payload(1, 0x07, 2.0, 9.0)
    r = decode_port(1, p, hw_protocol=7)
    assert r["protocol"] == "PD"
    assert r["voltage"] == 9.0
    assert "_raw_code" in r


def test_idle_overrides_everything():
    """端口空闲(V=0,I=0,in_use=0)时无论 hw_protocol 是什么都显示 idle."""
    p = make_test_payload(0, 0x00, 0.0, 0.0)
    r = decode_port_v2(1, p, hw_protocol=7)
    assert r["protocol"] == "idle"
    assert r["active"] is False
