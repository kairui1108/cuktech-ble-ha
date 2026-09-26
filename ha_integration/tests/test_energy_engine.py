"""Tests for the charge-limit energy engine (pure logic, no HA runtime)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger.energy_engine import (  # noqa: E402
    AdaptiveEnergyIntegrator,
    ChargeEndDetector,
    ChargeLimitTracker,
    END_REASON_LINK_LOSS,
    END_REASON_LOW_POWER,
    END_REASON_SHUTDOWN,
    END_REASON_UNKNOWN,
    END_REASON_UNPLUG,
    END_REASON_USER_OFF,
    LIMIT_MODE_ALWAYS,
    LIMIT_MODE_ONCE,
    PORTS,
    PortEnergyState,
    limit_reached,
    normalize_charge_limit,
)


@pytest.fixture
def tracker():
    return ChargeLimitTracker()


def _charge(tracker, port, seconds, power_w=60.0, step=1.0, t0=1000.0):
    """Simulate a steady charge session: V*I == power_w for `seconds` samples."""
    v, i = 20.0, power_w / 20.0
    t = t0
    for n in range(seconds):
        tracker.ingest(port, v, i, True, t)
        t += step
    return t


class TestLimitHelpers:
    """limit_reached / normalize_charge_limit — shared semantics with ble_server."""

    def test_limit_reached_disabled_when_zero_or_negative(self):
        assert limit_reached(10.0, 0) is False
        assert limit_reached(10.0, -5) is False

    def test_limit_reached_at_threshold_is_inclusive(self):
        # >= triggers; web UI shows "剩余 0" exactly at the threshold
        assert limit_reached(30.0, 30.0) is True
        assert limit_reached(29.9, 30.0) is False

    def test_normalize_rejects_nan_inf_negative_as_disabled(self):
        assert normalize_charge_limit(float("nan"))[0] == 0.0
        assert normalize_charge_limit(float("inf"))[0] == 0.0
        assert normalize_charge_limit(-3)[0] == 0.0

    def test_normalize_accepts_numeric_strings(self):
        assert normalize_charge_limit("30.5")[0] == 30.5

    def test_normalize_bad_mode_falls_back_to_default(self):
        assert normalize_charge_limit(10, "nonsense")[1] == LIMIT_MODE_ONCE
        assert normalize_charge_limit(10, None)[1] == LIMIT_MODE_ONCE
        assert normalize_charge_limit(10, "ALWAYS")[1] == LIMIT_MODE_ALWAYS

    def test_normalize_junk_value_never_raises(self):
        for junk in (None, "abc", {}, [], object()):
            wh, mode = normalize_charge_limit(junk)
            assert wh == 0.0 and mode == LIMIT_MODE_ONCE

    def test_max_limit_wh_bound_matches_server(self):
        """The bound lives in const.py and the number entity must use it.

        (It used to be defined in both const.py and energy_engine.py.)
        """
        from custom_components.cuktech_charger.const import MAX_LIMIT_WH
        from custom_components.cuktech_charger.number import CuktechChargeLimit
        assert MAX_LIMIT_WH == 1000.0
        assert CuktechChargeLimit._attr_native_max_value == MAX_LIMIT_WH


class TestIntegrator:
    """Trapezoidal integration ported from ble_server/energy.py."""

    def test_first_sample_seeds_baseline_without_accumulating(self):
        es = PortEnergyState()
        AdaptiveEnergyIntegrator().update(es, 20.0, 3.0, 1000.0)
        assert es.total_wh == 0.0
        assert es.last_power == 60.0

    def test_steady_power_integrates_to_expected_wh(self):
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        # 60W sustained for 3600s == 60Wh
        for n in range(3601):
            integ.update(es, 20.0, 3.0, 1000.0 + n)
        assert es.total_wh == pytest.approx(60.0, rel=1e-3)

    def test_retained_stale_frame_does_not_inflate(self):
        """The load-bearing MAX_GAP_SEC case.

        On subscribe we get the broker's retained sample; its wall-clock delta
        vs the next real frame can be minutes. Without the gap guard this one
        frame alone would integrate a huge phantom trapezoid and could trip a
        limit instantly.
        """
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        integ.update(es, 20.0, 3.0, 1000.0)          # retained, old
        integ.update(es, 20.0, 3.0, 1000.0 + 7200)   # 2h later, real? no — gap
        assert es.total_wh == 0.0

    def test_clock_rollback_accumulates_nothing(self):
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        integ.update(es, 20.0, 3.0, 5000.0)
        integ.update(es, 20.0, 3.0, 4000.0)  # dt < 0
        assert es.total_wh == 0.0

    def test_gap_just_inside_window_still_integrates(self):
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        integ.update(es, 20.0, 3.0, 1000.0)
        integ.update(es, 20.0, 3.0, 1000.0 + 30)
        assert es.total_wh > 0.0

    def test_trapezoid_uses_average_of_endpoints(self):
        """(a+b)/2 * dt_hours — ramp from 0W to 120W over 1h == 60Wh."""
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        integ.update(es, 20.0, 0.0, 0.0)      # 0W baseline
        integ.update(es, 20.0, 6.0, 30.0)     # 120W, 30s later -> 1Wh premature-free
        # (0 + 120)/2 * (30/3600)h = 0.5Wh — proves it averages, not endpoint
        assert es.total_wh == pytest.approx(0.5, rel=1e-6)

    def test_max_power_tracked(self):
        es = PortEnergyState()
        integ = AdaptiveEnergyIntegrator()
        integ.update(es, 20.0, 1.0, 0.0)
        integ.update(es, 20.0, 5.0, 1.0)
        integ.update(es, 20.0, 2.0, 2.0)
        assert es.max_power == 100.0


class TestChargeEndDetector:
    def test_needs_full_window_before_deciding(self):
        det = ChargeEndDetector()
        es = PortEnergyState()
        for n in range(299):
            det.update(0.0, 1000.0 + n)
        assert det.should_end_session(es, 1000.0 + 299) is False

    def test_sustained_low_power_ends_session(self):
        det = ChargeEndDetector()
        es = PortEnergyState()
        t = 1000.0
        for n in range(300):
            det.update(0.0, t)
            t += 1
        assert det.should_end_session(es, t) is False  # duration not reached
        for n in range(601):
            det.update(0.0, t)
            t += 1
        assert det.should_end_session(es, t) is True

    def test_recovering_power_cancels_pending_end(self):
        det = ChargeEndDetector()
        es = PortEnergyState()
        t = 1000.0
        for n in range(400):
            det.update(0.0, t)
            t += 1
        det.update(50.0, t)  # recovers
        t += 1
        assert det.should_end_session(es, t) is False

    def test_cooldown_suppresses_immediate_retrigger(self):
        """After reset(), readvances must wait out COOLDOWN_SEC before firing.

        This is why `ChargeLimitTracker.end_session` calls detector.reset(): it
        installs the cooldown so the just-ended session's decaying tail can't
        immediately retrigger another end detection.
        """
        det = ChargeEndDetector()
        es = PortEnergyState()
        det.reset(1000.0)
        assert det.should_end_session(es, 1000.0 + ChargeEndDetector.COOLDOWN_SEC - 1) is False
        # Fill the window past cooldown, then let the 10min low-power timer accrue.
        # The timer accumulates across successive calls (it is stamped on the
        # first qualifying call), so a single call can never end a session.
        t = 1000.0 + ChargeEndDetector.COOLDOWN_SEC + 1
        for n in range(ChargeEndDetector.WINDOW_SIZE):
            det.update(0.0, t)
            should = det.should_end_session(es, t)
            if n < ChargeEndDetector.WINDOW_SIZE - 1:
                assert should is False
            t += 1
        # Window full but duration not yet met
        assert det.should_end_session(es, t) is False
        t += ChargeEndDetector.LOW_POWER_DURATION_SEC + 1
        det.update(0.0, t)
        assert det.should_end_session(es, t) is True


class TestTrackerConfig:
    def test_default_all_ports_disabled(self, tracker):
        for p in PORTS:
            assert tracker.get_limit(p).wh == 0.0
            assert tracker.get_limit(p).mode == LIMIT_MODE_ONCE
            assert tracker.remaining_wh(p) is None

    def test_set_limit_normalizes_and_enables(self, tracker):
        tracker.set_limit("c1", 25, LIMIT_MODE_ALWAYS)
        cfg = tracker.get_limit("c1")
        assert cfg.wh == 25.0
        assert cfg.mode == LIMIT_MODE_ALWAYS

    def test_set_limit_zero_disables(self, tracker):
        tracker.set_limit("c1", 25, LIMIT_MODE_ALWAYS)
        tracker.set_limit("c1", 0)
        assert tracker.get_limit("c1").wh == 0.0
        assert tracker.remaining_wh("c1") is None

    def test_set_mode_preserves_wh(self, tracker):
        tracker.set_limit("c1", 30, LIMIT_MODE_ONCE)
        tracker.set_mode("c1", LIMIT_MODE_ALWAYS)
        assert tracker.get_limit("c1").wh == 30.0
        assert tracker.get_limit("c1").mode == LIMIT_MODE_ALWAYS

    def test_set_unknown_port_is_noop(self, tracker):
        assert tracker.set_limit("nope", 10).wh == 0.0

    def test_set_limit_rearms_fired_flag(self, tracker):
        """User raising/lowering the limit mid-session must re-enable enforcement."""
        tracker.set_limit("c1", 1, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600 * 2)
        assert tracker.due_cutoffs(1000.0 + 7200) == ["c1"]
        tracker.set_limit("c1", 500, LIMIT_MODE_ALWAYS)
        assert tracker.has_fired("c1") is False
        assert tracker.due_cutoffs(1000.0 + 7300) == []

    def test_snapshot_roundtrip(self, tracker):
        tracker.set_limit("c1", 12.5, LIMIT_MODE_ALWAYS)
        tracker.set_limit("a", 0)
        snap = tracker.snapshot_limits()
        other = ChargeLimitTracker()
        other.load_limits(snap)
        assert other.get_limit("c1").wh == 12.5
        assert other.get_limit("c1").mode == LIMIT_MODE_ALWAYS
        assert other.get_limit("a").wh == 0.0

    def test_load_limits_tolerates_junk(self, tracker):
        other = ChargeLimitTracker()
        other.load_limits({"c1": {"wh": "not-a-number", "mode": "bogus"},
                           "c2": None, "unknown_port": {"wh": 5}})
        assert other.get_limit("c1").wh == 0.0
        assert other.get_limit("c1").mode == LIMIT_MODE_ONCE
        assert other.get_limit("c2").wh == 0.0

    def test_load_limits_non_dict_ignored(self, tracker):
        tracker.load_limits("garbage")
        tracker.load_limits(None)
        assert tracker.get_limit("c1").wh == 0.0


class TestTrackerSession:
    def test_session_starts_and_accumulates(self, tracker):
        tracker.ingest("c1", 20.0, 3.0, True, 1000.0)
        for n in range(1, 3601):
            tracker.ingest("c1", 20.0, 3.0, True, 1000.0 + n)
        assert tracker.is_charging("c1") is True
        assert tracker.session_wh("c1") == pytest.approx(60.0, rel=1e-3)

    def test_idle_port_never_starts_session(self, tracker):
        """Energy still integrates (trapezoid runs on every sample), but no
        session starts while the port reports inactive — mirrors Python, where
        session_wh is only zeroed at the *next* session start (ble_manager:1583),
        not at close. That delay is intentional: the close-time reading of
        session_wh is what gets published/recorded.
        """
        tracker.ingest("c1", 20.0, 3.0, False, 1000.0)
        tracker.ingest("c1", 20.0, 3.0, False, 1001.0)
        assert tracker.is_charging("c1") is False
        # no session => the sensor reports 0 even though raw Wh accrued
        assert tracker.session_wh("c1") == pytest.approx(60.0 / 3600, rel=1e-3)

    def test_current_below_start_threshold_does_not_start(self, tracker):
        tracker.ingest("c1", 20.0, 0.05, True, 1000.0)
        tracker.ingest("c1", 20.0, 0.05, True, 1001.0)
        assert tracker.is_charging("c1") is False

    def test_recent_end_raises_start_threshold(self, tracker):
        """Within 60s of a previous end, threshold goes 0.1 -> 0.3A."""
        _charge(tracker, "c1", 10, power_w=20.0)
        tracker.ingest("c1", 0.0, 0.0, False, 1011.0)  # ends
        # 0.2A would start a fresh session, but not one within the recent window
        tracker.ingest("c1", 20.0, 0.2, True, 1012.0)
        assert tracker.is_charging("c1") is False

    def test_inactive_ends_session_as_user_off(self, tracker):
        tracker.set_limit("c1", 999, LIMIT_MODE_ONCE)
        _charge(tracker, "c1", 5)
        tracker.ingest("c1", 0.0, 0.0, False, 2000.0)
        assert tracker.is_charging("c1") is False
        # once-mode consumed only here if reason is a real termination
        assert tracker.get_limit("c1").wh == 0.0

    def test_low_current_debounce_ends_session(self, tracker):
        _charge(tracker, "c1", 5)
        t = 2000.0
        for n in range(tracker.LOW_CURRENT_N + 5):
            tracker.ingest("c1", 20.0, 0.05, True, t)
            t += 1
        assert tracker.is_charging("c1") is False

    def test_total_survives_session_end_session_wh_awaits_next_start(self, tracker):
        """session_wh is intentionally NOT cleared at session end.

        Python does the same (ble_manager:1583 zeroes it at the *next* start)
        so the final reading survives long enough to be published/recorded.
        Practical effect for the HA sensor: it shows the completed session's
        energy until the next session begins.
        """
        _charge(tracker, "c1", 3600)
        total_before = tracker.total_wh("c1")
        tracker.ingest("c1", 0.0, 0.0, False, 5000.0)
        assert tracker.total_wh("c1") == pytest.approx(total_before, rel=1e-6)
        assert tracker.session_wh("c1") == pytest.approx(60.0, rel=1e-3)
        # next session starts -> energy resets
        tracker.ingest("c1", 20.0, 3.0, True, 6000.0)
        assert tracker.session_wh("c1") == 0.0


class TestTrackerCutoff:
    def test_no_cutoff_without_limit(self, tracker):
        _charge(tracker, "c1", 7200)
        assert tracker.due_cutoffs(9999.0) == []

    def test_cutoff_when_threshold_reached(self, tracker):
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)  # 60Wh delivered
        assert tracker.due_cutoffs(1000.0 + 3600) == ["c1"]

    def test_cutoff_suppressed_while_disconnected_idle(self, tracker):
        tracker.set_limit("c1", 0.001, LIMIT_MODE_ALWAYS)
        tracker.ingest("c1", 20.0, 3.0, False, 1000.0)  # active=False
        assert tracker.due_cutoffs(1001.0) == []

    def test_cutoff_is_idempotent_within_retry_window(self, tracker):
        """Command in flight must not spam the broker."""
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)
        t = 1000.0 + 3600
        assert tracker.due_cutoffs(t) == ["c1"]
        assert tracker.due_cutoffs(t + 1) == []
        assert tracker.due_cutoffs(t + tracker.LIMIT_RETRY_SEC - 1) == []

    def test_retry_after_expired_window(self, tracker):
        """Port still charging past the window => command failed, allow retry."""
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)
        t = 1000.0 + 3600
        tracker.due_cutoffs(t)
        assert tracker.due_cutoffs(t + tracker.LIMIT_RETRY_SEC + 1) == ["c1"]

    def test_independent_per_port(self, tracker):
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)
        _charge(tracker, "c2", 3600)
        assert tracker.due_cutoffs(9999.0) == ["c1"]

    def test_remaining_wh_clamps_at_zero(self, tracker):
        tracker.set_limit("c1", 10, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)  # 60Wh >> 10Wh
        assert tracker.remaining_wh("c1") == 0.0


class TestOnceVsAlways:
    def test_once_consumed_on_real_termination(self, tracker):
        tracker.set_limit("c1", 999, LIMIT_MODE_ONCE)
        _charge(tracker, "c1", 5)
        tracker.end_session("c1", END_REASON_USER_OFF, 2000.0)
        assert tracker.get_limit("c1").wh == 0.0

    def test_once_preserved_on_link_loss(self, tracker):
        """A BLE hiccup must not silently drop the user's freshly set limit."""
        tracker.set_limit("c1", 999, LIMIT_MODE_ONCE)
        tracker.end_session("c1", END_REASON_LINK_LOSS, 2000.0)
        assert tracker.get_limit("c1").wh == 999.0

    def test_once_preserved_on_shutdown(self, tracker):
        tracker.set_limit("c1", 999, LIMIT_MODE_ONCE)
        tracker.end_session("c1", END_REASON_SHUTDOWN, 2000.0)
        assert tracker.get_limit("c1").wh == 999.0

    def test_always_survives_session_end(self, tracker):
        tracker.set_limit("c1", 999, LIMIT_MODE_ALWAYS)
        tracker.end_session("c1", END_REASON_USER_OFF, 2000.0)
        assert tracker.get_limit("c1").wh == 999.0

    def test_always_rearmed_by_new_session_start(self, tracker):
        tracker.set_limit("c1", 30, LIMIT_MODE_ALWAYS)
        _charge(tracker, "c1", 3600)
        tracker.due_cutoffs(1000.0 + 3600)
        assert tracker.has_fired("c1") is True
        # cutoff -> port goes inactive, then a new session begins
        tracker.ingest("c1", 0.0, 0.0, False, 5000.0)
        tracker.ingest("c1", 20.0, 3.0, True, 5100.0)
        assert tracker.has_fired("c1") is False
        assert tracker.get_limit("c1").wh == 30.0

    def test_disabled_limit_never_consumed(self, tracker):
        tracker.set_limit("c1", 0, LIMIT_MODE_ONCE)
        tracker.end_session("c1", END_REASON_USER_OFF, 2000.0)
        assert tracker.get_limit("c1").wh == 0.0

    @pytest.mark.parametrize("reason", [END_REASON_UNPLUG, END_REASON_LOW_POWER,
                                        END_REASON_UNKNOWN, END_REASON_USER_OFF])
    def test_once_consumed_for_all_real_reasons(self, tracker, reason):
        tracker.set_limit("c1", 999, LIMIT_MODE_ONCE)
        tracker.end_session("c1", reason, 2000.0)
        assert tracker.get_limit("c1").wh == 0.0


class TestTotalsPersistence:
    def test_restore_totals_applies_valid_values(self, tracker):
        tracker.restore_totals({"c1": 123.4, "c2": "56.7"})
        assert tracker.total_wh("c1") == 123.4
        assert tracker.total_wh("c2") == 56.7

    def test_restore_totals_rejects_junk(self, tracker):
        tracker.restore_totals({"c1": "abc", "c2": None, "c3": float("nan"), "a": -1})
        assert tracker.total_wh("c1") == 0.0
        assert tracker.total_wh("c2") == 0.0
        assert tracker.total_wh("c3") == 0.0
        assert tracker.total_wh("a") == 0.0

    def test_restore_totals_ignores_non_dict(self, tracker):
        tracker.restore_totals("junk")
        assert tracker.total_wh("c1") == 0.0

    def test_session_wh_not_restored(self, tracker):
        """In-flight session energy is unrecoverable; restarts err toward
        over-charging rather than risking a premature cutoff."""
        tracker.restore_totals({"c1": 500.0})
        assert tracker.session_wh("c1") == 0.0

    def test_snapshot_totals_roundtrip(self, tracker):
        _charge(tracker, "c1", 3600)
        snap = tracker.snapshot_totals()
        other = ChargeLimitTracker()
        other.restore_totals(snap)
        assert other.total_wh("c1") == pytest.approx(snap["c1"], rel=1e-9)


class TestPortIndependence:
    def test_all_four_ports_tracked(self, tracker):
        for p in PORTS:
            tracker.set_limit(p, 5, LIMIT_MODE_ALWAYS)
            _charge(tracker, p, 900)  # 15Wh each
        due = tracker.due_cutoffs(9999.0)
        assert sorted(due) == sorted(PORTS)

    def test_ingest_unknown_port_ignored(self, tracker):
        tracker.ingest("nope", 20.0, 3.0, True, 1000.0)  # must not raise
