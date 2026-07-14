"""Tests for ha_server.py - HTTP API endpoints."""
import asyncio
import sys
import json
import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from history import PortHistory


@pytest.fixture
def real_history():
    """Create a real PortHistory with temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    h = PortHistory(db_path=db_path, retention_days=2)
    h.connect()
    # Insert some test data
    for i in range(5):
        h.record_port_data(1, {
            "voltage": 20.0 + i,
            "current": 2.0 + i * 0.1,
            "power": (20.0 + i) * (2.0 + i * 0.1),
            "active": True,
            "protocol": "PD",
        })
    yield h
    h.close()
    Path(db_path).unlink(missing_ok=True)

    def close(self):
        pass


class TestHandleChart:
    """Test chart API endpoint using real handler."""

    @pytest.fixture
    def server(self, real_history):
        """Create a Server instance with real history."""
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = real_history
        s._chart_cache = {}
        s._chart_cache_ttl = 10
        s._chart_cache_max = 50
        return s

    @pytest.mark.asyncio
    async def test_chart_returns_ok(self, server):
        """Test that chart endpoint returns ok=True."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"hours": "1", "interval": "20"}
        request.headers = {}

        result = await server.handle_chart(request)
        assert isinstance(result, web.Response)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert "labels" in body
        assert "datasets" in body

    @pytest.mark.asyncio
    async def test_chart_caching(self, server):
        """Test that chart data is cached."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"hours": "1", "interval": "20"}
        request.headers = {}

        await server.handle_chart(request)
        assert len(server._chart_cache) == 1

        await server.handle_chart(request)
        assert len(server._chart_cache) == 1

    @pytest.mark.asyncio
    async def test_chart_etag_304(self, server):
        """Test ETag 304 response."""
        from aiohttp import web
        request = AsyncMock()
        request.query = {"hours": "1", "interval": "20"}
        request.headers = {}

        result1 = await server.handle_chart(request)
        etag = result1.headers.get("ETag")

        request2 = AsyncMock()
        request2.query = {"hours": "1", "interval": "20"}
        request2.headers = {"If-None-Match": etag}

        result2 = await server.handle_chart(request2)
        assert result2.status == 304


class TestHandleStatistics:
    """Test statistics API endpoint."""

    @pytest.mark.asyncio
    async def test_statistics_returns_data(self, real_history):
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = real_history

        request = AsyncMock()
        request.match_info = {"port": "1"}
        request.query = {"hours": "24"}

        result = await s.handle_statistics(request)
        body = json.loads(result.body)
        assert body["ok"] is True

    @pytest.mark.asyncio
    async def test_statistics_invalid_port(self):
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = PortHistory()

        request = AsyncMock()
        request.match_info = {"port": "abc"}
        request.query = {"hours": "24"}

        result = await s.handle_statistics(request)
        assert result.status == 400


class TestHandleExport:
    """Test CSV export endpoint."""

    @pytest.mark.asyncio
    async def test_export_returns_csv(self, real_history):
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = real_history

        request = AsyncMock()
        request.match_info = {"port": "1"}
        request.query = {"hours": "24"}

        result = await s.handle_export(request)
        assert result.content_type == "text/csv"


class TestHandleLogLevel:
    """Test log level API endpoint."""

    @pytest.mark.asyncio
    async def test_get_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "GET"

        result = await s.handle_log_level(request)
        body = json.loads(result.body)
        assert "level" in body
        assert body["level"] in ["debug", "info", "warning", "error"]

    @pytest.mark.asyncio
    async def test_set_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"level": "debug"})

        result = await s.handle_log_level(request)
        body = json.loads(result.body)
        assert body["ok"] is True

    @pytest.mark.asyncio
    async def test_set_invalid_log_level(self):
        from ha_server import Server
        s = Server.__new__(Server)

        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"level": "invalid"})

        result = await s.handle_log_level(request)
        assert result.status == 400


class TestHandleProtocol:
    """Test /api/protocol endpoint."""

    @pytest.fixture
    def server(self):
        """Create a Server instance with mocked BLE state."""
        from ha_server import Server
        from state import ChargerState

        s = Server.__new__(Server)
        s.ble = MagicMock()
        s.ble.state = ChargerState()

        async def init_state():
            # Start with all protocols ON (c1/c2: 0x0F each, c3: 0x03, a: 0x03)
            await s.ble.state.update_protocol_extend(0x03030F0F)

        asyncio.run(init_state())
        s.ble.send_command = AsyncMock(return_value={"ok": True})
        return s

    @pytest.mark.asyncio
    async def test_protocol_toggle(self, server):
        """Test toggling a protocol switch."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "pd"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        # PD was ON, now should be OFF (state synced locally)
        assert server.ble.state.protocol_switches["c1"]["pd"] is False

    @pytest.mark.asyncio
    async def test_protocol_turn_on(self, server):
        """Test explicitly turning on a protocol switch."""
        # First turn it off
        await server.ble.state.update_protocol_extend(0x03030F0F & ~(1 << 0))

        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "pd", "action": "on"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c1"]["pd"] is True

    @pytest.mark.asyncio
    async def test_protocol_turn_off(self, server):
        """Test explicitly turning off a protocol switch."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c2", "protocol": "pps", "action": "off"})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c2"]["pps"] is False

    @pytest.mark.asyncio
    async def test_protocol_invalid_port(self, server):
        """Test invalid port returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c5", "protocol": "pd"})

        result = await server.handle_protocol(request)
        assert result.status == 400
        body = json.loads(result.body)
        assert body["ok"] is False

    @pytest.mark.asyncio
    async def test_protocol_invalid_protocol(self, server):
        """Test invalid protocol returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"port": "c1", "protocol": "invalid"})

        result = await server.handle_protocol(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_protocol_missing_params(self, server):
        """Test missing parameters returns error."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={})

        result = await server.handle_protocol(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_protocol_value_mode(self, server):
        """Test setting raw value."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={"value": 0})

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble.state.protocol_switches["c1"]["pd"] is False

    @pytest.mark.asyncio
    async def test_protocol_switches_mode(self, server):
        """Test bulk switch setting."""
        request = AsyncMock()
        request.json = AsyncMock(return_value={
            "switches": {
                "c1": {"pd": False, "pps": False, "ufcs": False},
                "c2": {"pd": False, "pps": False, "ufcs": False},
                "c3": {"ufcs": False, "scp": False},
                "a":  {"ufcs": False, "scp": False},
            }
        })

        result = await server.handle_protocol(request)
        body = json.loads(result.body)
        assert body["ok"] is True

    @pytest.mark.asyncio
    async def test_protocol_bad_json(self, server):
        """Test invalid JSON returns error."""
        import json as _json
        request = AsyncMock()
        request.json = AsyncMock(side_effect=_json.JSONDecodeError("bad", "", 0))

        result = await server.handle_protocol(request)
        assert result.status == 400
