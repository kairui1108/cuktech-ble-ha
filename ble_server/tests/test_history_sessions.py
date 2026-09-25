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


class TestProtocolStats:
    """充电协议聚合（/api/energy/protocols 的数据源）。"""

    def test_groups_by_protocol_sorted_by_wh(self, history):
        s1 = history.start_session(1, protocol="PD")
        s2 = history.start_session(2, protocol="PPS")
        s3 = history.start_session(1, protocol="PD")
        history.end_session(s1, 10.0, 50.0, 20.0, 2.0, 1800)
        history.end_session(s2, 5.0, 30.0, 10.0, 1.0, 900)
        history.end_session(s3, 2.0, 20.0, 5.0, 0.5, 600)

        stats = history.get_protocol_stats(period="all")
        assert [p["protocol"] for p in stats["protocols"]] == ["PD", "PPS"]
        pd = stats["protocols"][0]
        assert pd["wh"] == 12.0
        assert pd["count"] == 2
        assert pd["peak_w"] == 50.0
        assert stats["total_wh"] == 17.0
        assert stats["session_count"] == 3

    def test_blank_protocol_falls_back_to_unknown(self, history):
        """protocol 为空的历史行归到 unknown，否则各协议之和与总量对不上。"""
        sid = history.start_session(1, protocol="")
        history.end_session(sid, 4.0, 20.0, 5.0, 0.5, 600)
        stats = history.get_protocol_stats(period="all")
        assert [p["protocol"] for p in stats["protocols"]] == ["unknown"]
        assert stats["total_wh"] == 4.0

    def test_zero_wh_sessions_excluded_like_energy_stats(self, history):
        """与 get_energy_stats 同口径：total_wh=0 的行不算（崩溃遗留/空会话）。"""
        sid = history.start_session(1, protocol="PD")
        history.end_session(sid, 0.0, 0.0, 0.0, 0.0, 60)
        stats = history.get_protocol_stats(period="all")
        assert stats["protocols"] == []
        assert stats["session_count"] == 0
        assert stats["total_wh"] == 0

    def test_totals_match_energy_stats(self, history):
        """同一周期下，协议分布与按端口分布的合计必须一致。"""
        for port, proto, wh in ((1, "PD", 3.0), (2, "PD", 7.0), (3, "UFCS", 1.5)):
            sid = history.start_session(port, protocol=proto)
            history.end_session(sid, wh, 20.0, 5.0, 0.5, 600)
        assert (history.get_protocol_stats("all")["total_wh"]
                == history.get_energy_stats("all")["total_wh"])

    def test_connection_closed_safe(self, history):
        history.close()
        assert history.get_protocol_stats(period="today")["protocols"] == []


class TestPeriodWindow:
    """周期窗口口径（前端下拉的 today/yesterday/week/month/all）。"""

    def test_today_starts_at_midnight_with_open_end(self, history):
        start, end = history._period_window("today")
        assert end is None
        assert 0 < time.time() - start < 86400

    def test_yesterday_is_exactly_one_day_wide(self, history):
        start, end = history._period_window("yesterday")
        assert end is not None
        assert end - start == pytest.approx(86400)
        assert start < end <= time.time()

    def test_week_and_month_are_rolling_windows(self, history):
        """week/month 是滚动 7/30 天（不是自然周月），前端文案必须跟着这么写。"""
        now = time.time()
        assert now - history._period_window("week")[0] == pytest.approx(7 * 86400, abs=5)
        assert now - history._period_window("month")[0] == pytest.approx(30 * 86400, abs=5)

    def test_unknown_period_starts_at_zero(self, history):
        assert history._period_window("bogus") == (0.0, None)


class TestStatisticsZeroHandling:
    """0 是合法读数，不能当成"没有数据"返回 null。"""

    def test_zero_voltage_is_reported_as_zero_not_null(self, history):
        history.record_port_data(1, {
            "voltage": 0.0, "current": 0.0, "power": 0.0,
            "active": False, "protocol": "idle",
        })
        stats = history.get_statistics(1, hours=24)
        assert stats["samples"] == 1
        assert stats["voltage"]["min"] == 0.0
        assert stats["voltage"]["avg"] == 0.0
        assert stats["current"]["avg"] == 0.0
        assert stats["power"]["avg"] == 0.0

    def test_no_samples_returns_empty_dict(self, history):
        assert history.get_statistics(3, hours=1) == {"port": 3, "hours": 1, "samples": 0}


class TestSessionCsvExport:
    """单个会话的 CSV 导出（会话详情浮层的"导出 CSV"）。"""

    def test_exports_only_that_session_points(self, history):
        s1 = history.start_session(1, protocol="PD")
        history.record_charge_point(s1, 20.0, 1.5, 30.0, "PD")
        history.record_charge_point(s1, 20.1, 1.6, 32.2, "PD")
        history.end_session(s1, 1.2, 32.2, 20.0, 1.5, 300)
        s2 = history.start_session(2, protocol="PPS")
        history.record_charge_point(s2, 10.0, 0.5, 5.0, "PPS")
        history.end_session(s2, 0.1, 5.0, 10.0, 0.5, 60)

        csv_text = history.export_session_csv(s1)
        assert "session 1" in csv_text
        assert "port c1" in csv_text          # 端口名走 PORT_NAMES（c1/c2/c3/a）
        assert "port c2" not in csv_text      # 另一次会话不能混进来
        assert "PPS" not in csv_text
        assert csv_text.count(",20.0,1.5,30.0,") == 1   # 本次会话的那一行
        # 表头统一
        assert "timestamp,datetime,voltage,current,power,protocol" in csv_text

    def test_reports_session_summary_in_comment_rows(self, history):
        sid = history.start_session(3, protocol="UFCS")
        history.record_charge_point(sid, 5.0, 0.4, 2.0, "UFCS")
        history.end_session(sid, 0.5, 2.0, 5.0, 0.4, 120)
        csv_text = history.export_session_csv(sid)
        assert "energy_wh 0.5" in csv_text
        assert "peak_power_w 2.0" in csv_text
        assert "duration_s 120" in csv_text

    def test_unknown_session_returns_empty(self, history):
        assert history.export_session_csv(99999) == ""
        assert history.export_session_csv(0) == ""

    def test_connection_closed_safe(self, history):
        history.close()
        assert history.export_session_csv(1) == ""

    def test_export_route_serves_csv(self):
        """路由层：/api/sessions/{id}/export 返回 text/csv 且带下载文件名。"""
        import inspect
        import ha_server
        src = inspect.getsource(ha_server)
        assert 'add_get("/api/sessions/{id}/export"' in src
        assert "handle_session_export" in src
