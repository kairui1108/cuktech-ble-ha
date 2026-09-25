"""Tests for energy tracker."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from energy import AdaptiveEnergyIntegrator, PortEnergyState, ChargeEndDetector
from energy import (limit_reached, normalize_charge_limit,
                    MAX_LIMIT_WH, LIMIT_MODES, DEFAULT_LIMIT_MODE)


def test_basic_accumulation():
    """20V * 1A for 30s = 0.167Wh."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 30.0)
    expected = 20.0 * 30 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_basic_accumulation")


def test_zero_power():
    """Zero current = zero energy."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 0.0, 0.0)
    integ.update(state, 20.0, 0.0, 10.0)
    assert state.total_wh == 0.0, f"Expected 0Wh, got {state.total_wh}"
    print("PASS: test_zero_power")


def test_irregular_interval_skipped():
    """Gap > 30s should be skipped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 20.0, 1.0, 50.0)
    assert state.total_wh == 0.0, f"Expected 0Wh after skip, got {state.total_wh}"
    print("PASS: test_irregular_interval_skipped")


def test_overshoot_protection():
    """10x power spike should be capped."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    integ.update(state, 20.0, 1.0, 0.0)
    integ.update(state, 200.0, 1.0, 1.0)
    assert state.total_wh < 200, f"Expected capped, got {state.total_wh}"
    print("PASS: test_overshoot_protection")


def test_multiple_accumulation():
    """10 points at 5s intervals = 45s total."""
    integ = AdaptiveEnergyIntegrator()
    state = PortEnergyState()
    for i in range(10):
        integ.update(state, 20.0, 1.0, i * 5.0)
    expected = 20.0 * 45 / 3600
    assert abs(state.total_wh - expected) < 0.01, f"Expected ~{expected:.4f}Wh, got {state.total_wh}"
    print("PASS: test_multiple_accumulation")


def test_charge_end_detection():
    """Average power < 1W for 10+ minutes should trigger end."""
    det = ChargeEndDetector()
    state = PortEnergyState()
    base = 1000000.0
    # Fill window with low-power data (0.4W avg, below 1W threshold)
    for i in range(400):
        det.update(0.4, base + i)
    # First call: sets _low_power_start
    det.should_end_session(state, base + 400)
    # Add more data 600s later
    for i in range(400, 1000):
        det.update(0.4, base + i)
    # Second call: 600+ seconds of low power → trigger end (need >600)
    assert det.should_end_session(state, base + 1001), "Should detect charge complete"
    print("PASS: test_charge_end_detection")


def test_cooldown():
    det = ChargeEndDetector()
    state = PortEnergyState()
    base = 1000000.0
    for i in range(400):
        det.update(0.4, base + i)
    det.should_end_session(state, base + 400)
    for i in range(400, 1001):
        det.update(0.4, base + i)
    assert det.should_end_session(state, base + 1001)
    det.on_session_end(base + 1001)
    assert not det.should_end_session(state, base + 1001 + 10)
    print("PASS: test_cooldown")


def test_window_not_full_no_trigger():
    """Window < 300 entries should never trigger, even with low power."""
    det = ChargeEndDetector()
    state = PortEnergyState()
    base = 1000000.0
    for i in range(299):
        det.update(0.1, base + i)
    assert not det.should_end_session(state, base + 300), "Window not full should not trigger"
    print("PASS: test_window_not_full_no_trigger")


def test_cooldown_after_restart():
    """After on_session_end, low_power_start resets, new low-power cycle must restart from zero."""
    det = ChargeEndDetector()
    state = PortEnergyState()
    base = 1000000.0
    # Fill window and trigger
    for i in range(1000):
        det.update(0.4, base + i)
    det.should_end_session(state, base + 400)
    assert det.should_end_session(state, base + 1001)
    # End session
    det.on_session_end(base + 1001)
    # Immediately restart with low power — should NOT trigger because _low_power_start was reset
    for i in range(1000):
        det.update(0.4, base + 1002 + i)
    assert not det.should_end_session(state, base + 1002 + 100), \
        "After cooldown reset, new low-power cycle must not trigger early"
    # But after 600+ seconds of sustained low power, should trigger
    assert det.should_end_session(state, base + 1002 + 100 + 601), \
        "Should trigger after 600s of sustained low power (timer starts at first check)"
    print("PASS: test_cooldown_after_restart")


def test_high_power_resets_low_power_timer():
    """High power burst resets the low-power countdown."""
    det = ChargeEndDetector()
    state = PortEnergyState()
    base = 1000000.0
    for i in range(400):
        det.update(0.1, base + i)
    det.should_end_session(state, base + 400)
    # Continue low power, then high power burst near the window tail
    for i in range(401, 1400):
        det.update(0.1, base + i)
    # Multiple high-power entries to push avg > 1W in last 300
    for i in range(1400, 1450):
        det.update(50.0, base + i)
    for i in range(1451, 1500):
        det.update(0.1, base + i)
    # Should NOT trigger because high power reset the timer
    assert not det.should_end_session(state, base + 1501), \
        "High power burst should reset countdown"
    print("PASS: test_high_power_resets_low_power_timer")


# ── Charge limit ──

def test_limit_reached_disabled_when_non_positive():
    """limit_wh <= 0 means disabled — never reached, whatever the session energy."""
    assert not limit_reached(100.0, 0.0)
    assert not limit_reached(100.0, -5.0)
    print("PASS: test_limit_reached_disabled_when_non_positive")


def test_limit_reached_boundaries():
    """Exact equality counts as reached; below does not."""
    assert not limit_reached(29.99, 30.0)
    assert limit_reached(30.0, 30.0)
    assert limit_reached(30.01, 30.0)
    print("PASS: test_limit_reached_boundaries")


def test_normalize_charge_limit_valid():
    assert normalize_charge_limit(30, "always") == (30.0, "always")
    assert normalize_charge_limit(0.5, "once") == (0.5, "once")
    assert normalize_charge_limit("12.5", "ONCE") == (12.5, "once")
    assert normalize_charge_limit(MAX_LIMIT_WH, "always") == (MAX_LIMIT_WH, "always")
    print("PASS: test_normalize_charge_limit_valid")


def test_normalize_charge_limit_rejects_dirty_input():
    """NaN/inf/负数/非数值一律回落禁用；mode 非法回落默认 mode。"""
    for bad in (float("nan"), float("inf"), float("-inf"), -1, "-3", None,
                "abc", [], {}):
        wh, mode = normalize_charge_limit(bad, "always")
        assert wh == 0.0, f"{bad!r} should normalize to disabled, got {wh}"
        assert mode == "always", "valid mode must survive a bad wh value"
    for bad_mode in (None, "", "sometimes", 123):
        wh, mode = normalize_charge_limit(30, bad_mode)
        assert wh == 30.0, "valid wh must survive a bad mode"
        assert mode == DEFAULT_LIMIT_MODE, f"{bad_mode!r} -> {mode}"
    assert DEFAULT_LIMIT_MODE in LIMIT_MODES
    print("PASS: test_normalize_charge_limit_rejects_dirty_input")


if __name__ == "__main__":
    test_basic_accumulation()
    test_zero_power()
    test_irregular_interval_skipped()
    test_overshoot_protection()
    test_multiple_accumulation()
    test_charge_end_detection()
    test_cooldown()
    test_limit_reached_disabled_when_non_positive()
    test_limit_reached_boundaries()
    test_normalize_charge_limit_valid()
    test_normalize_charge_limit_rejects_dirty_input()
    print("\nAll tests passed!")
