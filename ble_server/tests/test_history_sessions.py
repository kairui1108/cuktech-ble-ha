"""Tests for history.py - Charge session management."""
import time
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestChargeSessions:
    """Test charge session CRUD operations."""

    def test_start_session(self, history):
        """Starting a session returns a valid session_id."""
        sid = history.start_session(1, protocol="PD")
        assert sid > 0, f"Expected valid session_id, got {sid}"

    def test_start_session_returns_incrementing_ids(self, history):
        """Consecutive session starts return different IDs."""
        sid1 = history.start_session(1)
        sid2 = history.start_session(2)
        assert sid2 > sid1

    def test_start_session_stores_port_and_protocol(self, history):
        """Session metadata is stored correctly after ending."""
        sid = history.start_session(2, protocol="QC")
        history.end_session(sid, 1.0, 50.0, 20.0, 1.0, 600)
        sessions, _ = history.get_sessions(port=2, period="all")
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 1
        assert match[0]["port"] == 2
        assert match[0]["protocol"] == "QC"
        assert match[0]["start_time"] > 0

    def test_record_charge_point(self, history):
        """Recording points makes them queryable."""
        sid = history.start_session(1)
        ts = time.time()
        history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        history.record_charge_point(sid, 20.1, 2.5, 50.25, "PD")
        points = history.get_session_points(sid)
        assert len(points) == 2
        assert points[0]["voltage"] == 20.0
        assert points[1]["voltage"] == 20.1

    def test_record_charge_point_no_session_id(self, history):
        """record_charge_point with session_id=0 is silently ignored."""
        history.record_charge_point(0, 20.0, 2.5, 50.0, "PD")
        # No error expected

    def test_end_session_updates_stats(self, history):
        """Ending a session correctly stores stats."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 2.5, 50.0, "PD")
        history.end_session(sid, total_wh=1.5, peak_power_w=50.0,
                            avg_voltage=20.0, avg_current=2.5, duration_sec=3600)
        sessions, _ = history.get_sessions(port=1, period="all")
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 1
        s = match[0]
        assert s["total_wh"] == 1.5
        assert s["peak_power_w"] == 50.0
        assert s["avg_voltage"] == 20.0
        assert s["avg_current"] == 2.5
        assert s["duration_sec"] == 3600
        assert s["end_time"] is not None

    def test_end_session_calculates_avg_power(self, history):
        """end_session calculates avg_power_w from total_wh and duration."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=20.0, peak_power_w=50.0,
                            avg_voltage=20.0, avg_current=2.5, duration_sec=3600)
        sessions, _ = history.get_sessions(port=1, period="all")
        match = [s for s in sessions if s["id"] == sid]
        assert abs(match[0]["avg_power_w"] - 20.0) < 0.1

    def test_end_session_zero_duration(self, history):
        """end_session with duration=0 should set avg_power=0."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=10.0, peak_power_w=50.0,
                            avg_voltage=0, avg_current=0, duration_sec=0)
        sessions, _ = history.get_sessions(port=1, period="all")
        match = [s for s in sessions if s["id"] == sid]
        assert match[0]["avg_power_w"] == 0

    def test_delete_session_removes_points(self, history):
        """Deleting a session removes both session and points."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 2.5, 50.0)
        history.delete_session(sid)
        points = history.get_session_points(sid)
        assert points == []
        sessions, _ = history.get_sessions(port=1, period="all")
        match = [s for s in sessions if s["id"] == sid]
        assert len(match) == 0

    def test_get_sessions_filters_by_port(self, history):
        """get_sessions returns only sessions for specified port."""
        s1 = history.start_session(1)
        s2 = history.start_session(2)
        s3 = history.start_session(1)
        for s in [s1, s2, s3]:
            history.end_session(s, 1.0, 30.0, 20.0, 1.0, 600)
        sessions_c1, _ = history.get_sessions(port=1, period="all")
        sessions_c2, _ = history.get_sessions(port=2, period="all")
        assert len(sessions_c1) == 2
        assert len(sessions_c2) == 1

    def test_get_sessions_pagination(self, history):
        """get_sessions respects limit and offset."""
        sids = []
        for _ in range(5):
            sid = history.start_session(1)
            history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
            sids.append(sid)
        page1, total = history.get_sessions(period="all", limit=2, offset=0)
        page2, _ = history.get_sessions(period="all", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert total == 5

    def test_get_sessions_excludes_zero_wh(self, history):
        """Sessions with total_wh=0 are excluded from results."""
        sid = history.start_session(1)
        history.end_session(sid, total_wh=0, peak_power_w=0,
                            avg_voltage=0, avg_current=0, duration_sec=0)
        sessions, total = history.get_sessions(port=1, period="all")
        assert len(sessions) == 0
        assert total == 0

    def test_get_sessions_period_today(self, history):
        """period='today' filters by current day."""
        sid = history.start_session(1)
        history.end_session(sid, 1.0, 30.0, 20.0, 1.0, 600)
        sessions, total = history.get_sessions(port=1, period="today")
        assert total >= 1  # today should include sessions started today

    def test_get_energy_stats_basic(self, history):
        """get_energy_stats returns aggregated stats."""
        s1 = history.start_session(1)
        s2 = history.start_session(2)
        history.end_session(s1, 10.0, 50.0, 20.0, 2.0, 1800)
        history.end_session(s2, 5.0, 30.0, 10.0, 1.0, 900)
        stats = history.get_energy_stats(period="all")
        assert stats["total_wh"] == 15.0
        assert stats["session_count"] == 2
        assert stats["total_duration_sec"] == 2700
        assert "by_port" in stats
        assert "1" in stats["by_port"]
        assert "2" in stats["by_port"]

    def test_get_energy_stats_peak(self, history):
        """peak_power_w comes from charge_sessions.peak_power_w."""
        sid = history.start_session(1)
        history.record_charge_point(sid, 20.0, 5.0, 100.0)  # 100W in points
        history.end_session(sid, 10.0, 50.0, 20.0, 1.0, 1800)  # session says 50W peak
        stats = history.get_energy_stats(period="all")
        assert stats["peak_power_w"] == 50.0  # uses charge_sessions.peak_power_w

    def test_get_energy_stats_empty(self, history):
        """get_energy_stats with no data returns zeros."""
        stats = history.get_energy_stats(period="all")
        assert stats["total_wh"] == 0
        assert stats["session_count"] == 0
        assert stats["total_duration_sec"] == 0
        assert stats["avg_power_w"] == 0

    def test_get_energy_stats_by_port(self, history):
        """by_port aggregation shows per-port totals."""
        s1 = history.start_session(1)
        s2 = history.start_session(1)
        s3 = history.start_session(2)
        history.end_session(s1, 10.0, 50.0, 20.0, 2.0, 1800)
        history.end_session(s2, 5.0, 30.0, 10.0, 1.0, 900)
        history.end_session(s3, 3.0, 20.0, 5.0, 0.5, 600)
        stats = history.get_energy_stats(period="all")
        assert stats["by_port"]["1"]["wh"] == 15.0
        assert stats["by_port"]["1"]["count"] == 2
        assert stats["by_port"]["2"]["wh"] == 3.0
        assert stats["by_port"]["2"]["count"] == 1

    def test_connection_closed_safe(self, history):
        """Methods are safe to call after closing the connection."""
        history.close()
        assert history.start_session(1) == 0
        history.record_charge_point(1, 20.0, 2.5, 50.0)  # no error
        history.end_session(1, 1.0, 30.0, 20.0, 1.0, 600)
        history.delete_session(1)
        points = history.get_session_points(1)
        assert points == []
