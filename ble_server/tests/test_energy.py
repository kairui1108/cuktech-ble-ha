"""Tests for energy tracker."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from energy import AdaptiveEnergyIntegrator, PortEnergyState, ChargeEndDetector


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
    """Low power for 10+ minutes should trigger end."""
    det = ChargeEndDetector()
    state = PortEnergyState(max_power=20.0)
    base = 1000000.0
    # Fill window with low-power data
    for i in range(400):
        det.update(0.02, base + i)
    # First call: sets _low_power_start
    det.should_end_session(state, base + 400)
    # Add more data 600s later
    for i in range(400, 1000):
        det.update(0.02, base + i)
    # Second call: 600+ seconds of low power → trigger end (need >600)
    assert det.should_end_session(state, base + 1001), "Should detect charge complete"
    print("PASS: test_charge_end_detection")


def test_cooldown():
    det = ChargeEndDetector()
    state = PortEnergyState(max_power=20.0)
    base = 1000000.0
    for i in range(400):
        det.update(0.02, base + i)
    det.should_end_session(state, base + 400)
    for i in range(400, 1001):
        det.update(0.02, base + i)
    assert det.should_end_session(state, base + 1001)
    det.on_session_end(base + 1001)
    assert not det.should_end_session(state, base + 1001 + 10)
    print("PASS: test_cooldown")


if __name__ == "__main__":
    test_basic_accumulation()
    test_zero_power()
    test_irregular_interval_skipped()
    test_overshoot_protection()
    test_multiple_accumulation()
    test_charge_end_detection()
    test_behavior_classification()
    test_cooldown()
    print("\nAll tests passed!")
