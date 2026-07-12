"""
协议检测模块测试 - 米家协议号对齐
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
    print("=== RawPortData 解析 ===")
    raw = RawPortData.from_payload(make_test_payload(1, 0x07, 2.0, 9.0))
    assert raw.voltage == 9.0
    assert raw.current == 2.0
    assert raw.code == 0x07
    print(f"  ✓ 9.0V/2.0A code=0x07")

    assert RawPortData.from_payload(bytes(5)) is None
    print(f"  ✓ 短数据返回 None\n")


def test_mijia_protocol_names():
    """验证协议名称与米家完全一致."""
    print("=== 米家协议名称 ===")
    expected = {
        0: "idle", 1: "5V", 2: "5V", 3: "QC",
        4: "AFC", 5: "FCP", 6: "SCP",
        7: "PD", 8: "PPS", 9: "PPS", 10: "UFCS",
    }
    for num, name in expected.items():
        actual = get_mijia_protocol_name(num)
        assert actual == name, f"协议{num}: 期望'{name}', 得到'{actual}'"
        print(f"  protocol {num:2d} → {name}")
    assert get_mijia_protocol_name(99) == "Unknown (0x63)"
    print(f"  ✓ 未知协议号处理正确\n")


def test_estimate_protocol():
    """测试协议号估算."""
    print("=== 协议号估算 ===")
    cases = [
        # (name, piid, code, volt, expected_proto)
        ("C1 PD  20V",       1, 0x07, 20.0, 7),
        ("C1 PPS 8.6V",      1, 0x0A, 8.6,  8),
        ("C1 PPS 9.2V",      1, 0x0A, 9.2,  8),
        ("C1 PD  5V",        1, 0x07, 5.0,  7),
        ("C1 PPS explicit",  1, 0x08, 8.4,  8),
        ("C2 PD  20V",       2, 0x03, 20.1, 7),
        ("C3 QC  12V",       3, 0x60, 12.1, 3),
        ("C3 QC  9V",        3, 0x80, 9.0,  3),
        ("C3 PD  20V",       3, 0x80, 20.0, 7),
        ("C3 QC  explicit",  3, 0x70, 9.0,  3),
        ("C3 5V ",           3, 0x60, 5.0,  1),
        ("USB-A 5V",         4, 0x60, 5.0,  1),
        ("USB-A QC 9V",      4, 0x60, 9.0,  3),
        ("USB-A QC 12V",     4, 0x60, 12.0, 3),
        ("idle",             1, 0x00, 0.0,  0),
    ]
    
    all_pass = True
    for name, piid, code, volt, expected in cases:
        p = make_test_payload(1 if volt > 0 else 0, code, 1.0, volt)
        raw = RawPortData.from_payload(p)
        proto_num = estimate_protocol_number(piid, raw)
        result_name = get_mijia_protocol_name(proto_num)
        ok = "✓" if proto_num == expected else "✗"
        if ok == "✗":
            all_pass = False
            print(f"  {ok} {name}: proto={proto_num} ({result_name}), 期望 proto={expected}")
        else:
            print(f"  {ok} {name}: proto={proto_num} ({result_name})")
    
    print(f"\n  {'全部通过' if all_pass else '有失败'}\n")


def test_decode_port_v2():
    """测试完整解码函数."""
    print("=== decode_port_v2 完整测试 ===")
    cases = [
        ("C1 PD  5V",        1, 0x07, 2.0, 5.0, "PD"),
        ("C1 PPS 9.2V",      1, 0x0A, 1.2, 9.2, "PPS"),
        ("C2 PD  20V",       2, 0x03, 1.5, 20.1, "PD"),
        ("C3 QC  12V",       3, 0x60, 0.3, 12.1, "QC"),
        ("USB-A 5V",         4, 0x60, 1.0, 5.0, "5V"),
        ("idle",             1, 0x00, 0.0, 0.0, "idle"),
    ]
    
    for name, piid, code, cur, volt, expected in cases:
        p = make_test_payload(1 if volt > 0 else 0, code, cur, volt)
        r = decode_port_v2(piid, p)
        ok = "✓" if r["protocol"] == expected else "✗"
        print(f"  {ok} {name}: '{r['protocol']}' (期望 '{expected}') conf={r['_confidence']:.0%}")
    
    print()


def test_decode_port_compat():
    """测试向后兼容包装器."""
    print("=== decode_port 兼容性 ===")
    p = make_test_payload(1, 0x07, 2.0, 9.0)
    r = decode_port(1, p)
    assert "protocol" in r
    assert "voltage" in r
    assert "_confidence" in r
    assert r["voltage"] == 9.0
    print(f"  ✓ 兼容接口正常: {r['protocol']}\n")


def test_port_type():
    """测试端口类型."""
    print("=== 端口类型 ===")
    assert get_port_type(1) == PortType("type_c_12")
    assert get_port_type(2) == PortType("type_c_12")
    assert get_port_type(3) == PortType("type_c_3")
    assert get_port_type(4) == PortType("usb_a")
    print("  ✓ 端口类型正确\n")


if __name__ == "__main__":
    print("=" * 60)
    print("CUKTECH 协议检测测试 - 米家协议号对齐")
    print("=" * 60)
    
    test_raw_port_data()
    test_mijia_protocol_names()
    test_estimate_protocol()
    test_decode_port_v2()
    test_decode_port_compat()
    test_port_type()
    
    print("✅ 测试完成")
