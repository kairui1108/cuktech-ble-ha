"""
集成测试：验证协议检测引擎
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from state import decode_port
from state_protocol_v2 import decode_port_v2, get_mijia_protocol_name, estimate_protocol_number, RawPortData


def make_test_payload(in_use: bool, code: int, current: float, voltage: float) -> bytes:
    padding = bytes([0] * 11)
    return padding + bytes([1 if in_use else 0, code, int(current * 10), int(voltage * 10)])


class TestBackwardCompatibility:
    def test_import_and_basic_call(self):
        print("✓ 测试1: 基本导入成功")
        payload = make_test_payload(True, 0x07, 3.0, 9.0)
        result = decode_port(1, payload)
        assert result is not None
        for key in ("voltage", "current", "power", "active", "protocol"):
            assert key in result, f"缺少 {key}"
        print(f"   结果: {result['protocol']} V={result['voltage']}V I={result['current']}A")
        print("✅ 通过\n")


class TestEdgeCaseFixes:
    def test_pps_detection(self):
        print("✓ 测试2: PPS 检测")
        payload = make_test_payload(True, 0x0A, 2.5, 8.6)
        v2 = decode_port_v2(1, payload)
        unified = decode_port(1, payload)
        assert v2["protocol"] == "PPS", f"期望 PPS, 得到 {v2['protocol']}"
        assert unified["protocol"] == "PPS", f"统一接口应为 PPS, 得到 {unified['protocol']}"
        print(f"   V2: {v2['protocol']} conf={v2['_confidence']:.0%}")
        print(f"   统一: {unified['protocol']}")
        print("✅ 通过\n")

    def test_c3_qc_detection(self):
        print("✓ 测试3: C3 QC 检测")
        payload = make_test_payload(True, 0x60, 0.3, 12.1)
        result = decode_port(3, payload)
        assert result["protocol"] == "QC", f"期望 QC, 得到 {result['protocol']}"
        print(f"   C3 12.1V → {result['protocol']}")
        print("✅ 通过\n")

    def test_usb_a_detection(self):
        print("✓ 测试4: USB-A 检测")
        p5v = make_test_payload(True, 0x60, 1.0, 5.0)
        p9v = make_test_payload(True, 0x60, 2.0, 9.0)
        assert decode_port(4, p5v)["protocol"] == "5V"
        assert decode_port(4, p9v)["protocol"] == "QC"
        print(f"   5.0V → 5V, 9.0V → QC")
        print("✅ 通过\n")


class TestExceptionSafety:
    def test_invalid_input(self):
        print("✓ 测试5: 无效输入")
        assert decode_port(1, bytes([0]*5)) is None
        assert decode_port(1, bytes([])) is None
        print("✅ 通过\n")


class TestDataFormatConsistency:
    def test_output_fields(self):
        print("✓ 测试6: 输出字段")
        cases = [
            (True, 0x01, 3.0, 9.0),
            (True, 0x60, 1.0, 5.0),
            (False, 0x00, 0.0, 0.0),
        ]
        required = {"voltage", "current", "power", "active", "protocol"}
        for i, (in_use, code, cur, volt) in enumerate(cases):
            p = make_test_payload(in_use, code, cur, volt)
            r = decode_port(i % 4 + 1, p)
            assert r is not None
            assert required - set(r.keys()) == set()
            print(f"   ✓ 用例{i}: 字段完整")
        print("✅ 通过\n")

    def test_numeric_precision(self):
        print("✓ 测试7: 数值精度")
        # int(2.567*10)=25 → 2.5A, int(8.678*10)=86 → 8.6V
        p = make_test_payload(True, 0x01, 2.5, 8.6)
        r = decode_port(1, p)
        assert r["voltage"] == 8.6
        assert r["current"] == 2.5
        exp_power = round(8.6 * 2.5, 1)
        assert abs(r["power"] - exp_power) < 0.02
        print(f"   {r['voltage']}V / {r['current']}A → {r['power']}W")
        print("✅ 通过\n")


class TestProtocolNames:
    def test_mijia_names(self):
        print("✓ 测试8: 米家协议名称")
        names = {1:"5V", 3:"QC", 7:"PD", 8:"PPS", 10:"UFCS"}
        for num, name in names.items():
            assert get_mijia_protocol_name(num) == name
        print(f"   {names}")
        print("✅ 通过\n")


def run_all_tests():
    print("=" * 60)
    print("CUKTECH 协议检测集成测试")
    print("=" * 60 + "\n")

    tests = [
        TestBackwardCompatibility(),
        TestEdgeCaseFixes(),
        TestExceptionSafety(),
        TestDataFormatConsistency(),
        TestProtocolNames(),
    ]

    passed = 0
    failed = 0

    for tc in tests:
        for name in sorted([m for m in dir(tc) if m.startswith('test_')]):
            try:
                getattr(tc, name)()
                passed += 1
            except AssertionError as e:
                failed += 1
                print(f"❌ {tc.__class__.__name__}.{name}: {e}\n")
            except Exception as e:
                failed += 1
                print(f"💥 {tc.__class__.__name__}.{name}: {type(e).__name__}: {e}\n")

    print("=" * 60)
    print(f"📊 汇总: ✅ {passed} / ❌ {failed}")
    print("=" * 60)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
