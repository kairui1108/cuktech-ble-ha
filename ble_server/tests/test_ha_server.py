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


class TestSessionRecordingAPI:
    """/api/session-recording 开关接口（DB meta 持久化，即时生效）。"""

    @pytest.fixture
    def server(self, real_history):
        """Create a Server instance with real history and mock ble."""
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = real_history
        s.ble = MagicMock()
        s.ble.record_sessions = True
        s._status_cache_valid = False
        s._status_cache_bytes = None
        return s

    @pytest.mark.asyncio
    async def test_get_enabled(self, server):
        """GET 返回当前开关状态。"""
        request = AsyncMock()
        request.method = "GET"
        result = await server.handle_session_recording(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["enabled"] is True

    @pytest.mark.asyncio
    async def test_post_disables_and_persists(self, server):
        """POST 关闭后即时生效并持久化到 DB meta。"""
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"enabled": False})
        result = await server.handle_session_recording(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["enabled"] is False
        assert server.ble.record_sessions is False
        assert server.history.get_session_recording() is False  # 重启后仍生效
        assert server._status_cache_valid is False              # 状态缓存已失效

    @pytest.mark.asyncio
    async def test_post_requires_boolean(self, server):
        """enabled 非布尔值时拒绝。"""
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"enabled": "yes"})
        result = await server.handle_session_recording(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_post_enable_resumes_fake_sessions(self, server):
        """从关闭切换到打开时，触发正在充电端口立即转正记录。"""
        server.ble.record_sessions = False
        server.ble.resume_recording_sessions = MagicMock()
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"enabled": True})
        result = await server.handle_session_recording(request)
        body = json.loads(result.body)
        assert body["enabled"] is True
        server.ble.resume_recording_sessions.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_enable_same_state_no_resume(self, server):
        """已是开启状态再次开启时，不应重复触发转正。"""
        server.ble.resume_recording_sessions = MagicMock()
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"enabled": True})
        await server.handle_session_recording(request)
        server.ble.resume_recording_sessions.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_includes_session_recording(self, server):
        """/api/status 响应包含 session_recording 字段。"""
        server.state = MagicMock()
        server.state.to_dict = AsyncMock(return_value={})
        server.mqtt_client = None
        request = AsyncMock()
        request.query = {}
        result = await server.handle_status(request)
        body = json.loads(result.body)
        assert body["session_recording"] is True

    @pytest.mark.asyncio
    async def test_post_without_history_ok(self):
        """history 未连接/不可用时 POST 开关不崩溃，内存开关照常生效。"""
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = None
        s.ble = MagicMock()
        s.ble.record_sessions = True
        s.ble.resume_recording_sessions = MagicMock()
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"enabled": False})
        result = await s.handle_session_recording(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["enabled"] is False
        assert s.ble.record_sessions is False
        s.ble.resume_recording_sessions.assert_not_called()


class TestWebLanguageAPI:
    """/api/web-language 界面语言接口（DB meta 持久化，config.html 为唯一设置入口）。"""

    @pytest.fixture
    def server(self, real_history):
        """Create a Server instance with real history."""
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = real_history
        return s

    @pytest.mark.asyncio
    async def test_get_default_auto(self, server):
        """GET 返回当前语言偏好，默认 auto（跟随系统）。"""
        request = AsyncMock()
        request.method = "GET"
        result = await server.handle_web_language(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["language"] == "auto"

    @pytest.mark.asyncio
    async def test_post_explicit_and_persists(self, server):
        """POST 显式语言后即时生效并持久化到 DB meta。"""
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"language": "en"})
        result = await server.handle_web_language(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["language"] == "en"
        assert server.history.get_web_language() == "en"  # 重启后仍生效

    @pytest.mark.asyncio
    async def test_post_auto_resets(self, server):
        """切回 auto 后持久化为 auto。"""
        server.history.set_web_language("zh-CN")
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"language": "auto"})
        result = await server.handle_web_language(request)
        body = json.loads(result.body)
        assert body["language"] == "auto"
        assert server.history.get_web_language() == "auto"

    @pytest.mark.asyncio
    async def test_post_normalizes(self, server):
        """大小写/变体归一化为规范值。"""
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"language": "ZH-CN"})
        result = await server.handle_web_language(request)
        body = json.loads(result.body)
        assert body["language"] == "zh-CN"
        assert server.history.get_web_language() == "zh-CN"

    @pytest.mark.asyncio
    async def test_post_rejects_invalid_language(self, server):
        """不支持的语言值时拒绝。"""
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"language": "fr"})
        result = await server.handle_web_language(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_post_without_history_ok(self):
        """history 未连接/不可用时 POST 不崩溃。"""
        from ha_server import Server
        s = Server.__new__(Server)
        s.history = None
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value={"language": "en"})
        result = await s.handle_web_language(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["language"] == "en"


class TestChargeLimitsAPI:
    """/api/charge-limits 指定充电量后自动关断（DB meta 持久化，即时生效）。"""

    @pytest.fixture
    def server(self, real_history):
        """Server + 真实 BLEManager（限额状态在 manager 内存中）。"""
        from ha_server import Server
        from ble_manager import BLEManager
        from state import ChargerState

        s = Server.__new__(Server)
        s.history = real_history
        config = MagicMock()
        config.server.reconnect_base_delay = 1.0
        config.server.reconnect_max_delay = 300.0
        config.server.command_timeout = 10.0
        config.server.settings_refresh_interval = 60.0
        config.topic_status = "cuktech/charger/status"
        config.topic_settings = "cuktech/charger/settings"
        config.topic_port = "cuktech/charger/port"
        s.ble = BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff",
                           state=ChargerState(), config=config)
        s.ble.set_history(real_history)
        s._status_cache_valid = False
        s._status_cache_bytes = None
        return s

    def _post(self, body):
        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_get_defaults_all_disabled(self, server):
        request = AsyncMock()
        request.method = "GET"
        result = await server.handle_charge_limits(request)
        body = json.loads(result.body)
        assert body["ok"] is True
        assert set(body["limits"]) == {"c1", "c2", "c3", "a"}
        for entry in body["limits"].values():
            assert entry["wh"] == 0.0
            assert entry["mode"] == "once"
            assert entry["session_wh"] == 0.0
            assert entry["is_charging"] is False
            assert entry["fired"] is False

    @pytest.mark.asyncio
    async def test_post_single_port_persists_and_applies(self, server):
        result = await server.handle_charge_limits(
            self._post({"port": "c1", "wh": 30, "mode": "always"}))
        body = json.loads(result.body)
        assert body["ok"] is True
        assert body["limits"]["c1"]["wh"] == 30.0
        assert body["limits"]["c1"]["mode"] == "always"
        assert body["limits"]["c1"]["fired"] is False
        # 内存即时生效
        assert server.ble._charge_limits[1] == 30.0
        assert server.ble._limit_modes[1] == "always"
        # DB 持久化（重启后仍生效）
        assert server.history.get_charge_limits()["c1"] == {"wh": 30.0, "mode": "always"}
        assert server._status_cache_valid is False

    @pytest.mark.asyncio
    async def test_post_batch(self, server):
        result = await server.handle_charge_limits(
            self._post({"limits": {"c1": {"wh": 30, "mode": "always"},
                                   "a": {"wh": 10, "mode": "once"}}}))
        body = json.loads(result.body)
        assert body["limits"]["c1"]["wh"] == 30.0
        assert body["limits"]["a"]["wh"] == 10.0
        assert body["limits"]["a"]["mode"] == "once"
        assert body["limits"]["a"]["fired"] is False
        assert body["limits"]["c2"]["wh"] == 0.0   # 未提及的端口不动

    @pytest.mark.asyncio
    async def test_wh_zero_disables(self, server):
        await server.handle_charge_limits(self._post({"port": "c1", "wh": 30}))
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 0}))
        body = json.loads(result.body)
        assert body["limits"]["c1"]["wh"] == 0.0
        assert server.ble._charge_limits[1] == 0.0

    @pytest.mark.asyncio
    async def test_omitted_mode_keeps_existing(self, server):
        """只改 wh 时不应把已有 mode 重置为默认。"""
        await server.handle_charge_limits(self._post({"port": "c1", "wh": 30, "mode": "always"}))
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 40}))
        body = json.loads(result.body)
        assert body["limits"]["c1"]["wh"] == 40.0
        assert body["limits"]["c1"]["mode"] == "always"
        assert body["limits"]["c1"]["fired"] is False

    @pytest.mark.asyncio
    async def test_rejects_non_finite_and_negative(self, server):
        """NaN/inf/负数必须整体拒绝——静默归一成"禁用"会让用户以为设了限额。"""
        for bad in (float("nan"), float("inf"), float("-inf"), -1, -0.5):
            result = await server.handle_charge_limits(self._post({"port": "c1", "wh": bad}))
            assert result.status == 400, f"{bad!r} should be rejected"
        # 拒绝后配置未被改动
        assert server.ble._charge_limits[1] == 0.0
        assert server.history.get_charge_limits()["c1"]["wh"] == 0.0

    @pytest.mark.asyncio
    async def test_rejects_out_of_range(self, server):
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 1001}))
        assert result.status == 400
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": "abc"}))
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_accepts_boundary_values(self, server):
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 1000}))
        assert result.status == 200
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 0}))
        assert result.status == 200

    @pytest.mark.asyncio
    async def test_rejects_unknown_port(self, server):
        result = await server.handle_charge_limits(self._post({"port": "c9", "wh": 10}))
        assert result.status == 400
        result = await server.handle_charge_limits(
            self._post({"limits": {"c1": {"wh": 10}, "zz": {"wh": 5}}}))
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_rejects_invalid_mode(self, server):
        result = await server.handle_charge_limits(
            self._post({"port": "c1", "wh": 10, "mode": "sometimes"}))
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_rejects_missing_fields_and_bad_json(self, server):
        result = await server.handle_charge_limits(self._post({}))
        assert result.status == 400
        result = await server.handle_charge_limits(self._post({"port": "c1"}))
        assert result.status == 400   # wh 缺失
        result = await server.handle_charge_limits(self._post({"limits": {}}))
        assert result.status == 400

        request = AsyncMock()
        request.method = "POST"
        request.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
        result = await server.handle_charge_limits(request)
        assert result.status == 400

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_write_anything(self, server):
        """批量中任一项非法 → 整体拒绝，不留半写状态。"""
        result = await server.handle_charge_limits(
            self._post({"limits": {"c1": {"wh": 30}, "c2": {"wh": -5}}}))
        assert result.status == 400
        assert server.ble._charge_limits[1] == 0.0
        assert server.history.get_charge_limits()["c1"]["wh"] == 0.0

    @pytest.mark.asyncio
    async def test_get_reports_session_progress(self, server):
        """GET 带各端口本会话已充能量，前端可算剩余。"""
        await server.handle_charge_limits(self._post({"port": "c1", "wh": 30}))
        server.ble._energy_states[1].is_charging = True
        server.ble._energy_states[1].session_wh = 12.3456
        request = AsyncMock()
        request.method = "GET"
        result = await server.handle_charge_limits(request)
        body = json.loads(result.body)
        assert body["limits"]["c1"]["session_wh"] == 12.346
        assert body["limits"]["c1"]["is_charging"] is True
        assert body["limits"]["c1"]["wh"] == 30.0

    @pytest.mark.asyncio
    async def test_works_without_history(self, server):
        """history 不可用时仍能改内存配置（不写库），不抛异常。"""
        server.history = None
        result = await server.handle_charge_limits(self._post({"port": "c1", "wh": 30}))
        body = json.loads(result.body)
        assert body["ok"] is True
        assert server.ble._charge_limits[1] == 30.0

    @pytest.mark.asyncio
    async def test_once_limit_survives_restart_load(self, server):
        """重启加载配置不消费 once 限额（决策 C：重启不算会话终止）。"""
        await server.handle_charge_limits(
            self._post({"port": "c1", "wh": 30, "mode": "once"}))
        # 模拟新进程启动：从 DB 重新加载
        server.ble._charge_limits = {i: 0.0 for i in range(1, 5)}
        server.ble.set_charge_limits(server.history.get_charge_limits())
        assert server.ble._charge_limits[1] == 30.0


class TestShutdownPreservesOnceLimit:
    """on_shutdown 必须传 shutdown 原因：服务停止不是用户意图终止，不消费 once。

    实际生产关机路径是 aiohttp 的 on_shutdown 钩子（不是 ble.stop()），修复前
    它调用无参 _close_active_sessions() → reason=unknown → 误消费限额。
    """

    @pytest.mark.asyncio
    async def test_on_shutdown_keeps_once_limit(self):
        import ha_server
        from ble_manager import BLEManager, END_REASON_SHUTDOWN
        from state import ChargerState

        cfg = MagicMock()
        for k, v in dict(reconnect_base_delay=1.0, reconnect_max_delay=300.0,
                         command_timeout=10.0, settings_refresh_interval=60.0).items():
            setattr(cfg.server, k, v)
        cfg.topic_status = cfg.topic_settings = cfg.topic_port = "x"
        mgr = BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff",
                         state=ChargerState(), config=cfg)
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr.set_charge_limits({"c1": {"wh": 30.0, "mode": "once"}})
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 3.0
        es.session_start = time.time() - 60
        mgr.state.ports[1].voltage = 20.0
        mgr.state.ports[1].current = 2.0
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7

        # 复现 on_shutdown 的第一条语句
        mgr._close_active_sessions(END_REASON_SHUTDOWN)

        assert mgr._charge_limits[1] == 30.0, "服务停止不应消费 once 限额"
        assert es.is_charging is False, "会话仍应正常闭合"

    @pytest.mark.asyncio
    async def test_on_shutdown_source_passes_shutdown_reason(self):
        """静态断言：on_shutdown 里必须显式传 shutdown 原因（防止回归成无参调用）。"""
        import inspect
        import ha_server
        src = inspect.getsource(ha_server.on_shutdown)
        assert "_close_active_sessions(END_REASON_SHUTDOWN)" in src, \
            "on_shutdown 必须以 END_REASON_SHUTDOWN 关闭会话，否则关机误消费 once 限额"


class TestStaticCacheKeys:
    """静态缓存 key 的 Windows 路径兼容性（反斜杠 → 404 bug 回归测试）。"""

    def test_windows_separator_on_subdir_key(self):
        """Windows 上 Path.relative_to() 产出反斜杠，_static_cache_key 必须输出正斜杠，
        否则 /static/plugin_imgs/logo.png、/static/locales/zh-CN.js 会 404。"""
        import ha_server
        from pathlib import PureWindowsPath

        static = PureWindowsPath("C:/x/ble_server/web/static")
        files = [
            PureWindowsPath("C:/x/ble_server/web/static/app.js"),
            PureWindowsPath("C:/x/ble_server/web/static/plugin_imgs/logo.png"),
            PureWindowsPath("C:/x/ble_server/web/static/locales/zh-CN.js"),
        ]
        keys = {ha_server._static_cache_key(static, f) for f in files}
        assert "/static/app.js" in keys
        assert "/static/plugin_imgs/logo.png" in keys
        assert "/static/locales/zh-CN.js" in keys
        # 决不产生反斜杠 key（否则 request.path 精确匹配查不到）
        assert not any("\\" in k for k in keys)

    def test_cache_scan_subdir_files_present(self, tmp_path, monkeypatch):
        """_cache_static_files 全量扫描包含子目录文件，且 key 无反斜杠。"""
        import ha_server

        static = tmp_path / "web" / "static"
        (static / "plugin_imgs").mkdir(parents=True)
        (static / "locales").mkdir()
        (static / "app.js").write_text("var x = 1;", encoding="utf-8")
        (static / "plugin_imgs" / "logo.png").write_bytes(b"\x89PNG\r\n")
        (static / "locales" / "zh-CN.js").write_text("window.I18N_RESOURCES={};", encoding="utf-8")

        old_dir = ha_server.WEB_DIR
        old_cache = ha_server._static_cache
        try:
            ha_server.WEB_DIR = tmp_path / "web"
            ha_server._static_cache = {}
            ha_server._cache_static_files()
            keys = set(ha_server._static_cache.keys())
        finally:
            ha_server.WEB_DIR = old_dir
            ha_server._static_cache = old_cache

        assert "/static/app.js" in keys
        assert "/static/plugin_imgs/logo.png" in keys
        assert "/static/locales/zh-CN.js" in keys
        assert not any("\\" in k for k in keys)
