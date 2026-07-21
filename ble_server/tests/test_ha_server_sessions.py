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
