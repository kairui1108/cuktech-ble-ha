"""Tests for ha_server.py - Session/Energy API endpoints."""
import asyncio
import json
import pytest
import sys
import os
import time
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from history import PortHistory


@pytest.fixture
def history_with_sessions():
    """Create a PortHistory with some charge sessions."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    h = PortHistory(db_path=db_path, retention_days=2)
    h.connect()
    # Create two sessions
    s1 = h.start_session(1, protocol="PD")
    s2 = h.start_session(2, protocol="QC")
    h.record_charge_point(s1, 20.0, 2.5, 50.0, "PD")
    h.record_charge_point(s1, 20.1, 2.5, 50.25, "PD")
    h.record_charge_point(s2, 10.0, 1.0, 10.0, "QC")
    h.end_session(s1, 1.5, 50.25, 20.05, 2.5, 1800)
    h.end_session(s2, 0.5, 10.0, 10.0, 1.0, 600)
    yield h
    h.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def server_with_sessions(history_with_sessions):
    """Create a Server instance with session history and mocked BLE."""
    from ha_server import Server, reset_server
    reset_server()
    s = Server.__new__(Server)
    s.history = history_with_sessions
    s.ble = MagicMock()
    s.ble.get_live_session_data = MagicMock(return_value={})
    # 端口模式：无长供端口时窗口查询必返回 None（mock 默认值会污染 JSON 序列化）
    s.ble.permanent_ports = set()
    # /points 的缺省窗口取自内存保留时长（界面已无窗口控件，缺省=能给的都给了）
    s.ble.PERMANENT_RETAIN_SEC = 3600
    s.ble.get_permanent_session_window = MagicMock(return_value=None)
    s.ble.state = MagicMock()
    s.ble.state.ports = {}
    s.config = MagicMock()
    s.bemfa = None
    return s


class TestHandleSessions:
    """Test GET /api/sessions."""

    @pytest.mark.asyncio
    async def test_sessions_returns_list(self, server_with_sessions):
        """Returns list of sessions with pagination metadata."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert "sessions" in body
        assert "total" in body
        assert "page" in body
        assert "pages" in body

    @pytest.mark.asyncio
    async def test_sessions_count(self, server_with_sessions):
        """Returns correct number of sessions."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["total"] == 2

    @pytest.mark.asyncio
    async def test_sessions_port_filter(self, server_with_sessions):
        """Filtering by port returns only that port's sessions."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "port": "c1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        for s in body["sessions"]:
            assert s["port"] == 1

    @pytest.mark.asyncio
    async def test_sessions_page_parameter(self, server_with_sessions):
        """Page parameter is respected."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "1", "page": "1"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["page"] == 1
        assert len(body["sessions"]) == 1

    @pytest.mark.asyncio
    async def test_sessions_merge_live_data(self, server_with_sessions):
        """Active sessions get live energy data merged in."""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                1: {"session_id": 1, "session_wh": 2.0, "max_power": 60.0, "start_time": time.time() - 900},
            }
        )
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        # Session 1 should have updated values
        s1 = next(s for s in body["sessions"] if s["id"] == 1)
        assert s1["total_wh"] == 2.0  # Live data overrides DB
        assert s1["is_active"] is True

    @pytest.mark.asyncio
    async def test_sessions_inactive_mark(self, server_with_sessions):
        """Sessions not in live data are marked inactive."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        for s in body["sessions"]:
            assert s["is_active"] is False

    @pytest.mark.asyncio
    async def test_sessions_limit_capped(self, server_with_sessions):
        """Limit parameter is capped at 50."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "999"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["limit"] == 50

    @pytest.mark.asyncio
    async def test_sessions_empty_result(self, server_with_sessions):
        """No sessions matched returns empty list."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "yesterday"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["sessions"] == []
        assert body["total"] == 0

    @pytest.mark.asyncio
    async def test_sessions_live_session_not_in_db(self, server_with_sessions):
        """An active session that was filtered from DB (total_wh=0) is added."""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                3: {"session_id": 999, "session_wh": 0.5, "max_power": 30.0, "start_time": time.time() - 300},
            }
        )
        server_with_sessions.ble.state.ports = {3: MagicMock(voltage=15.0, current=0.5, protocol="PD")}
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        assert body["total"] == 3  # 2 DB + 1 live
        live_ids = [s["id"] for s in body["sessions"] if s.get("is_active")]
        assert 999 in live_ids


class TestHandleSessionPoints:
    """Test GET /api/sessions/{id}/points."""

    @pytest.mark.asyncio
    async def test_session_points_returns_points(self, server_with_sessions):
        """Returns charge points for a session."""
        from aiohttp import web
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert "points" in body
        assert len(body["points"]) == 2

    @pytest.mark.asyncio
    async def test_session_points_includes_fields(self, server_with_sessions):
        """Points contain timestamp, voltage, current, power, protocol."""
        from aiohttp import web
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        p = body["points"][0]
        assert "timestamp" in p
        assert "voltage" in p
        assert "current" in p
        assert "power" in p
        assert p["voltage"] == 20.0

    @pytest.mark.asyncio
    async def test_session_points_empty(self, server_with_sessions):
        """Session with no points returns empty list."""
        # Create a session with no points
        sid = server_with_sessions.history.start_session(3)
        server_with_sessions.history.end_session(sid, 0, 0, 0, 0, 0)
        from aiohttp import web
        request = AsyncMock()
        request.match_info = {"id": str(sid)}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert body["points"] == []

    @pytest.mark.asyncio
    async def test_session_points_permanent_flag_is_authoritative_and_bidirectional(
            self, server_with_sessions):
        """permanent 标记服务端权威且必须双向给出（含 false）。

        前端只"见到 true 才置位"的话，端口中途退出常供后详情浮层会一直挂着
        "长期供电"标题与"没有曲线"提示——所以 DB 回落分支也要回答这个问题。
        """
        from aiohttp import web
        sid = server_with_sessions.history.start_session(1)
        server_with_sessions.history.end_session(sid, 1.0, 30.0, 10.0, 3.0, 600)

        request = AsyncMock()
        request.match_info = {"id": str(sid)}
        request.query = {}
        server_with_sessions.ble.permanent_ports = set()
        body = json.loads((await server_with_sessions.handle_session_points(request)).body)
        assert body["permanent"] is False, "非常供会话必须显式给出 false"

        server_with_sessions.ble.permanent_ports = {1}
        body = json.loads((await server_with_sessions.handle_session_points(request)).body)
        assert body["permanent"] is True, "常供会话给出 true"

        server_with_sessions.ble.permanent_ports = set()

    @pytest.mark.asyncio
    async def test_session_points_downsample(self, server_with_sessions):
        """downsample parameter causes LTTB reduction."""
        # Add many points to make downsampling meaningful
        sid = server_with_sessions.history.start_session(4)
        for i in range(100):
            server_with_sessions.history.record_charge_point(
                sid, 20.0, 2.5, 50.0, "PD")
        server_with_sessions.history.end_session(sid, 1.0, 50.0, 20.0, 2.5, 600)
        from aiohttp import web
        request = AsyncMock()
        request.match_info = {"id": str(sid)}
        request.query = {"downsample": "10"}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert len(body["points"]) <= 10

    @pytest.mark.asyncio
    async def test_session_points_no_downsample(self, server_with_sessions):
        """Without downsample, all points are returned."""
        from aiohttp import web
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        # Session 1 has 2 points
        assert len(body["points"]) == 2


class TestHandleEnergyStats:
    """Test GET /api/energy/stats."""

    @pytest.mark.asyncio
    async def test_energy_stats_returns_stats(self, server_with_sessions):
        """Returns aggregated energy statistics."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        assert "total_wh" in body
        assert "session_count" in body
        assert "avg_power_w" in body
        assert "peak_power_w" in body
        assert "total_duration_sec" in body
        assert "by_port" in body

    @pytest.mark.asyncio
    async def test_energy_stats_totals(self, server_with_sessions):
        """Totals match session data."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        assert body["total_wh"] == 2.0  # 1.5 + 0.5
        assert body["session_count"] == 2
        assert body["total_duration_sec"] == 2400  # 1800 + 600

    @pytest.mark.asyncio
    async def test_energy_stats_merges_live(self, server_with_sessions):
        """Live session data is merged into stats."""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                1: {"session_id": 1, "session_wh": 0.3, "max_power": 55.0, "start_time": time.time() - 600},
            }
        )
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        # total_wh: 1.5 (session 1 from DB) + 0.5 (session 2 from DB) + 0.3 (live)
        # But live session 1 was already in DB with 1.5Wh — it gets summed: 1.5 + 0.3 + 0.5
        # Wait - live session 1 overlaps with DB session 1. The DB total includes session 1's 1.5Wh.
        # The live data adds another 0.3Wh on top. So total = 1.5 + 0.5 + 0.3 = 2.3
        assert body["total_wh"] == 2.3
        assert body["session_count"] == 3  # 2 DB + 1 live

    @pytest.mark.asyncio
    async def test_energy_stats_peak_from_live(self, server_with_sessions):
        """Peak power uses max of DB and live data."""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                1: {"session_id": 1, "session_wh": 2.0, "max_power": 100.0, "start_time": time.time() - 900},
            }
        )
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        # DB peak = 50.25, live peak = 100.0
        assert body["peak_power_w"] == 100.0

    @pytest.mark.asyncio
    async def test_energy_stats_by_port_live(self, server_with_sessions):
        """Live session data appears in by_port breakdown."""
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                3: {"session_id": 999, "session_wh": 0.5, "max_power": 30.0, "start_time": time.time() - 300},
            }
        )
        server_with_sessions.ble.state.ports = {}
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "all"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        assert "3" in body["by_port"]
        assert body["by_port"]["3"]["is_active"] is True

    @pytest.mark.asyncio
    async def test_energy_stats_empty(self, server_with_sessions):
        """No data in period returns zeros."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"period": "yesterday"}
        result = await server_with_sessions.handle_energy_stats(request)
        body = json.loads(result.body)
        assert body["total_wh"] == 0
        assert body["session_count"] == 0


class TestPermanentSessions:
    """长期供电端口在会话接口里的行为（标记 + 内存滑动窗口）。"""

    @pytest.mark.asyncio
    async def test_permanent_flag_on_active_row(self, server_with_sessions):
        """常供端口的活跃会话照常出现在列表里（能量/统计要计入），并带 permanent 标记。"""
        server_with_sessions.ble.permanent_ports = {1}
        server_with_sessions.ble.get_live_session_data = MagicMock(
            return_value={
                1: {"session_id": 1, "session_wh": 2.0, "max_power": 60.0,
                    "start_time": time.time() - 900, "permanent": True},
            })
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        s1 = next(s for s in body["sessions"] if s["id"] == 1)
        assert s1["permanent"] is True
        assert s1["is_active"] is True

    @pytest.mark.asyncio
    async def test_inactive_history_row_not_marked(self, server_with_sessions):
        """历史行不标常供：老会话是标记常供之前录的，曲线本来就在库里，
        标成常供会让详情看起来像"曲线丢了"。"""
        server_with_sessions.ble.permanent_ports = {1}
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        for s in body["sessions"]:
            if not s["is_active"]:
                assert s["permanent"] is False

    @pytest.mark.asyncio
    async def test_points_flag_no_curve_when_empty(self, server_with_sessions):
        """没有采样点（常供会话）时显式告诉前端"是没存曲线"，别让它像加载失败。"""
        sid = server_with_sessions.history.start_session(3)
        server_with_sessions.history.end_session(sid, 0.5, 5.0, 5.0, 1.0, 600)
        request = AsyncMock()
        request.match_info = {"id": str(sid)}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert body["points"] == []
        assert body["no_curve"] is True
        assert body["stats"]["total_wh"] == 0.5

    @pytest.mark.asyncio
    async def test_other_ports_not_marked(self, server_with_sessions):
        server_with_sessions.ble.permanent_ports = {1}
        request = AsyncMock()
        request.query = {"period": "all", "limit": "10"}
        result = await server_with_sessions.handle_sessions(request)
        body = json.loads(result.body)
        for s in body["sessions"]:
            if s["port"] != 1:
                assert s["permanent"] is False

    @pytest.mark.asyncio
    async def test_points_served_from_memory_window(self, server_with_sessions):
        """常供会话：曲线来自内存窗口 + 整个会话的 stats + window 元信息。"""
        now = time.time()
        server_with_sessions.ble.get_permanent_session_window = MagicMock(
            return_value={
                "permanent": True,
                "points": [{"timestamp": now - 60, "voltage": 20.0, "current": 1.0,
                            "power": 20.0, "protocol": "PD"}],
                "stats": {"session_id": 1, "port": 1, "total_wh": 42.0,
                          "avg_power_w": 20.0, "peak_power_w": 65.0,
                          "avg_voltage": 20.0, "avg_current": 1.0,
                          "duration_sec": 7200, "start_time": now - 7200},
                "window": {"retain_sec": 3600, "window_sec": 900, "from": now - 900,
                           "to": now, "available_from": now - 1800,
                           "available_to": now, "count": 1, "total_points": 1},
            })
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {"window": "900"}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert body["permanent"] is True
        assert len(body["points"]) == 1
        assert body["stats"]["total_wh"] == 42.0
        assert body["window"]["retain_sec"] == 3600
        server_with_sessions.ble.get_permanent_session_window.assert_called_once_with(
            1, 900.0, None)

    @pytest.mark.asyncio
    async def test_window_zero_falls_back_to_retain(self, server_with_sessions):
        """window=0 表示"全部保留区间"，由调用方换算成保留时长。"""
        server_with_sessions.ble.PERMANENT_RETAIN_SEC = 3600
        server_with_sessions.ble.get_permanent_session_window = MagicMock(return_value=None)
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {"window": "0"}
        await server_with_sessions.handle_session_points(request)
        server_with_sessions.ble.get_permanent_session_window.assert_called_once_with(
            1, 3600.0, None)

    @pytest.mark.asyncio
    async def test_bad_window_param_rejected(self, server_with_sessions):
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {"window": "abc"}
        result = await server_with_sessions.handle_session_points(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_normal_session_points_carry_stats(self, server_with_sessions):
        """普通会话响应附上整会话 stats（形状向后兼容，仅多字段）。"""
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}
        result = await server_with_sessions.handle_session_points(request)
        body = json.loads(result.body)
        assert len(body["points"]) == 2
        assert body["stats"]["total_wh"] == 1.5
        assert body["stats"]["duration_sec"] == 1800


class TestPointsDefaultWindow:
    """界面已去掉"窗口长度"控件：缺省就该给满内存保留时长（1 小时）+ 贴最新。"""

    @pytest.mark.asyncio
    async def test_default_window_is_full_retention(self, server_with_sessions):
        captured = {}

        def fake_window(sid, window_sec, to_ts):
            captured["window_sec"] = window_sec
            captured["to"] = to_ts
            return None      # 走 DB 回落分支即可，只关心入参

        server_with_sessions.ble.get_permanent_session_window = MagicMock(side_effect=fake_window)
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {}          # 不带 window / to
        await server_with_sessions.handle_session_points(request)

        assert captured["window_sec"] == float(server_with_sessions.ble.PERMANENT_RETAIN_SEC)
        assert captured["to"] is None, "缺省贴最新"

    @pytest.mark.asyncio
    async def test_window_zero_also_means_full_retention(self, server_with_sessions):
        captured = {}
        server_with_sessions.ble.get_permanent_session_window = MagicMock(
            side_effect=lambda sid, w, to: captured.update(window_sec=w) or None)
        request = AsyncMock()
        request.match_info = {"id": "1"}
        request.query = {"window": "0"}
        await server_with_sessions.handle_session_points(request)
        assert captured["window_sec"] == float(server_with_sessions.ble.PERMANENT_RETAIN_SEC)
