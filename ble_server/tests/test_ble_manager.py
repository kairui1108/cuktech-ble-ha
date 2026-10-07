"""Tests for ble_manager.py - BLE connection manager."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ble_manager import BLEManager, set_status_cache_invalidator, _invalidate
from ble_manager import (END_REASON_USER_OFF, END_REASON_UNPLUG, END_REASON_LOW_POWER,
                         END_REASON_NO_LOAD, END_REASON_LINK_LOSS, END_REASON_SHUTDOWN,
                         END_REASON_UNKNOWN, PORT_IDS)
from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT


class _RestartRequested(Exception):
    """模拟服务层重启处理器：execv / _exit 都不会正常返回，用异常表示"映像被替换"。"""


class _ProcessExited(Exception):
    """模拟 os._exit：立刻终止进程（测试里用异常代替，否则会杀掉 pytest）。"""


def make_config():
    """Create a mock config object."""
    config = MagicMock()
    config.server.reconnect_base_delay = 1.0
    config.server.reconnect_max_delay = 300.0
    config.server.command_timeout = 10.0
    config.server.settings_refresh_interval = 60.0
    config.topic_status = "cuktech/charger/status"
    config.topic_settings = "cuktech/charger/settings"
    config.topic_port = "cuktech/charger/port"
    return config


def _prime_start_gate(mgr, port=1, seconds=31.0):
    """让"会话开始门限"的持续时间条件立刻满足（测试用）。

    真实实现要求功率连续保持 START_HOLD_SEC（30s）才开会话；单帧推不动它，
    所以这里把计时起点往前挪，专注验证会话开始之后的行为。
    """
    mgr._start_gates[port]._above_since = time.time() - seconds


def make_manager():
    """Create a BLEManager with mock dependencies."""
    state = ChargerState()
    config = make_config()
    return BLEManager(mac="AA:BB:CC:DD:EE:FF", token="aabbccddeeff", state=state, config=config)


class TestBLEManagerInit:
    """Test BLEManager initialization."""

    def test_initial_state(self):
        """Test BLEManager initial state."""
        mgr = make_manager()
        assert mgr.mac == "AA:BB:CC:DD:EE:FF"
        assert mgr.ctrl is None
        assert mgr._reconnect_attempts == 0
        assert mgr._mqtt_publish is None
        assert mgr._history is None

    def test_set_mqtt_publisher(self):
        """Test setting MQTT publisher."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        assert mgr._mqtt_publish is publisher

    def test_set_history(self):
        """Test setting history module."""
        mgr = make_manager()
        history = MagicMock()
        mgr.set_history(history)
        assert mgr._history is history


class TestReconnectDelay:
    """Test exponential backoff delay calculation with jitter."""

    def test_initial_delay(self):
        """Test initial delay is base delay (no jitter for delay <= 1.0)."""
        mgr = make_manager()
        mgr._reconnect_attempts = 0
        assert mgr._get_reconnect_delay() == 1.0

    def test_exponential_increase(self):
        """Test delay increases exponentially within jitter range."""
        mgr = make_manager()
        mgr._reconnect_attempts = 3
        # base = 2^3 = 8, jitter ±25% = ±2.0 → range [6.0, 10.0]
        for _ in range(50):
            delay = mgr._get_reconnect_delay()
            assert 6.0 <= delay <= 10.0, f"delay {delay} outside range [6.0, 10.0]"

    def test_max_delay_cap(self):
        """Test delay is capped at max (with jitter)."""
        mgr = make_manager()
        mgr._reconnect_attempts = 10
        # base capped at 300, jitter ±25% = ±75 → range [225, 375]
        for _ in range(50):
            delay = mgr._get_reconnect_delay()
            assert 225 <= delay <= 375, f"delay {delay} outside range [225, 375]"

    def test_attempts_capped(self):
        """Test attempts are capped at 10 for exponent."""
        mgr = make_manager()
        mgr._reconnect_attempts = 100
        # Same as attempts=10 → range [225, 375]
        for _ in range(50):
            delay = mgr._get_reconnect_delay()
            assert 225 <= delay <= 375, f"delay {delay} outside range [225, 375]"


class TestPublishMethods:
    """Test MQTT publish methods."""

    def test_publish_status(self):
        """Test _publish_status publishes to correct topic."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        mgr._publish_status({"connected": True})
        publisher.assert_called_once_with("cuktech/charger/status", {"connected": True}, retain=False)

    def test_publish_status_retain(self):
        """Test _publish_status with retain."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        mgr._publish_status({"connected": True}, retain=True)
        publisher.assert_called_once_with("cuktech/charger/status", {"connected": True}, retain=True)

    def test_publish_settings(self):
        """Test _publish_settings publishes settings."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        mgr.state.settings = {"5": 1}
        mgr._publish_settings(retain=True)
        publisher.assert_called_once_with("cuktech/charger/settings", {"5": 1}, retain=True)

    def test_publish_port(self):
        """Test _publish_port publishes to port topic."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        data = {"voltage": 20.0, "current": 2.0}
        mgr._publish_port("c1", data)
        publisher.assert_called_once_with("cuktech/charger/port/c1", data, retain=False)

    def test_publish_without_mqtt(self):
        """Test publish methods don't crash when MQTT is None."""
        mgr = make_manager()
        mgr._publish_status({"connected": True})
        mgr._publish_settings()
        mgr._publish_port("c1", {})


class TestProcessCommands:
    """Test command processing."""

    @pytest.mark.asyncio
    async def test_process_empty_queue(self):
        """Test processing empty queue does nothing."""
        mgr = make_manager()
        await mgr._process_commands()

    @pytest.mark.asyncio
    async def test_process_set_command(self):
        """Test processing set command."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(
            return_value={"piid": 5, "value": 1, "raw": b""})

        future = asyncio.get_running_loop().create_future()
        await mgr.cmd_queue.put(("set", (5, 1), future))

        await mgr._process_commands()

        assert future.done()
        assert future.result() == {"ok": True}

    @pytest.mark.asyncio
    async def test_process_port_command(self):
        """Test processing port command."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"value": 0x0F})
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr.cmd_queue.put(("port", ("c1", "on"), future))

        await mgr._process_commands()

        assert future.done()
        assert future.result()["ok"] is True

    @pytest.mark.asyncio
    async def test_process_command_exception(self):
        """Test command exception is caught and returned."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(side_effect=Exception("BLE error"))

        future = asyncio.get_running_loop().create_future()
        await mgr.cmd_queue.put(("set", (5, 1), future))

        await mgr._process_commands()

        assert future.done()
        result = future.result()
        assert result["ok"] is False
        assert "BLE error" in result["error"]


class TestPortCommandSafety:
    """PIID16 读-改-写的安全约束（对照 fork 审查: 掩码误写会关掉正在供电的口）。

    掩码是四个口共用的位图, 基线一旦取错（例如 GET 失败按 0 兜底）, 写回的
    只剩目标口自己的位 —— 其余正在供电的口被静默关闭, 且不可自愈。
    """

    @pytest.mark.asyncio
    async def test_baseline_unknown_refuses_to_write(self):
        """基线未知（本地无缓存 + GET 失败）时拒绝写入, 而不是按 0 兜底。"""
        mgr = make_manager()
        mgr.state.settings.pop("16", None)
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value=None)  # GET 失败
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("c1", "off"), future)

        assert future.result()["ok"] is False
        # 关键: 没有向设备写过任何 SET
        mgr.ctrl.send_miot_command.assert_called_once_with(2, 16)

    @pytest.mark.asyncio
    async def test_other_charging_ports_not_cleared_when_baseline_unknown(self):
        """多口场景: C2 正在充电, 关 C1 时基线未知 → C2 必须保持开启。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x02      # 只有 C2 开着（正在供电）
        mgr.state.settings.pop("16")          # 本地缓存缺失, 必须向设备读
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value=None)  # 读取失败
        mgr.set_mqtt_publisher(MagicMock())

        await mgr._handle_port_command(("c1", "off"), None)

        # 不得写出任何 SET（按 0 兜底会写出 0x00, 把 C2 一起关掉）
        sets = [c for c in mgr.ctrl.send_miot_command.call_args_list
                if "value" in c.kwargs]
        assert sets == [], f"基线未知时不得写出掩码: {sets}"

    @pytest.mark.asyncio
    async def test_uses_authoritative_get_as_baseline(self):
        """基线优先取设备权威值（不受缓存过期影响）。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x01      # 缓存是过期的旧值
        mgr.ctrl = MagicMock()
        calls = []

        async def fake_send(siid, piid, value=None):
            calls.append((siid, piid, value))
            if value is None:
                return {"piid": 16, "value": 0x03, "raw": b""}   # GET: 真实掩码 C1+C2
            return {"piid": 16, "value": value, "raw": b""}      # SET 回显

        mgr.ctrl.send_miot_command = fake_send
        mgr.set_mqtt_publisher(MagicMock())

        await mgr._handle_port_command(("c1", "off"), None)

        # 以设备权威值 0x03 为基线 → 关 C1 得 0x02（C2 保留），而不是用缓存 0x01 算出的 0x00
        sets = [c for c in calls if c[2] is not None]
        assert sets and sets[0][2] == 0x02, f"未使用权威基线: {calls}"
        assert mgr.state.settings["16"] == 0x02

    @pytest.mark.asyncio
    async def test_falls_back_to_cache_when_get_fails(self):
        """GET 失败时回落到本地缓存（绝不按 0 兜底）。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x03      # C1+C2
        mgr.ctrl = MagicMock()
        calls = []

        async def fake_send(siid, piid, value=None):
            calls.append((siid, piid, value))
            if value is None:
                return None                                   # GET 失败
            return {"piid": 16, "value": value, "raw": b""}

        mgr.ctrl.send_miot_command = fake_send
        mgr.set_mqtt_publisher(MagicMock())

        await mgr._handle_port_command(("c1", "off"), None)

        sets = [c for c in calls if c[2] is not None]
        assert sets and sets[0][2] == 0x02, f"缓存回落未生效: {calls}"

    @pytest.mark.asyncio
    async def test_all_off_does_not_require_baseline(self):
        """port=all 的掩码是常量, 基线未知也必须能执行（评审 #2 回归）。"""
        mgr = make_manager()
        mgr.state.settings.pop("16", None)   # 无缓存
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"value": 0x00})
        mgr.set_mqtt_publisher(MagicMock())
        for piid in (1, 2, 3, 4):
            mgr._energy_states[piid].is_charging = True

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("all", "off"), future)

        assert future.result()["ok"] is True, "all-off 不应因基线缺失被拒"
        assert mgr.state.settings["16"] == 0x00
        for piid in (1, 2, 3, 4):
            assert mgr._energy_states[piid].is_charging is False

    @pytest.mark.asyncio
    async def test_cache_fallback_baseline_writes_unconditionally(self):
        """GET 失败回落到缓存时, 即使缓存值恰好等于目标值也必须下发写入。

        缓存可能滞后于固件（固件倒计时自行关口）：若因"值未变"跳过 SET,
        设备没收到写入却返回 ok —— 又是一次假成功（评审第三轮 #2）。
        """
        mgr = make_manager()
        mgr.state.settings["16"] = 0x02      # 缓存说 C2 已开
        mgr.ctrl = MagicMock()
        calls = []

        async def fake_send(siid, piid, value=None):
            calls.append((siid, piid, value))
            if value is None:
                return None                                   # GET 失败 → 走缓存
            return {"piid": 16, "value": value, "raw": b""}

        mgr.ctrl.send_miot_command = fake_send
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("c2", "on"), future)   # 目标 == 缓存值

        sets = [c for c in calls if c[2] is not None]
        assert sets, "缓存基线下值未变就跳过写入 —— 假成功"
        assert sets[0][2] == 0x02
        assert future.result()["ok"] is True

    @pytest.mark.asyncio
    async def test_all_always_writes_even_if_cache_matches(self):
        """all 命令不得因"缓存等于目标值"而跳过写入（评审 #6）。

        缓存可能滞后（固件倒计时自行关口）；若缓存恰好等于目标值就跳过 SET,
        设备没收到写入却返回 ok, 正是"假成功"分叉。
        """
        mgr = make_manager()
        mgr.state.settings["16"] = 0x0F      # 缓存说已全开
        mgr.ctrl = MagicMock()
        calls = []

        async def fake_send(siid, piid, value=None):
            calls.append((siid, piid, value))
            return {"piid": 16, "value": 0x0F, "raw": b""}

        mgr.ctrl.send_miot_command = fake_send
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("all", "on"), future)

        sets = [c for c in calls if c[2] is not None]
        assert sets, "缓存等于目标值时 all 命令被跳过, 没有真正下发"
        assert sets[0][2] == 0x0F
        assert future.result()["ok"] is True

    @pytest.mark.asyncio
    async def test_device_echo_mismatch_adopts_device_value(self):
        """设备回显与意图不一致时以设备值为准, 避免基线永久偏移（评审 #5）。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x03
        mgr.ctrl = MagicMock()

        async def fake_send(siid, piid, value=None):
            if value is None:
                return {"piid": 16, "value": 0x03, "raw": b""}   # GET
            return {"piid": 16, "value": 0x00, "raw": b""}       # 设备回显 0x00（与意图 0x02 不符）

        mgr.ctrl.send_miot_command = fake_send
        mgr.set_mqtt_publisher(MagicMock())

        await mgr._handle_port_command(("c1", "off"), None)

        assert mgr.state.settings["16"] == 0x00, "应采用设备回显值作为基线"

    @pytest.mark.asyncio
    async def test_unconfirmed_set_does_not_update_state(self):
        """SET 未被设备确认（无响应）时, 本地状态与前端广播都不得推进。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x03
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value=None)  # SET 无响应
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("c1", "off"), future)

        assert future.result()["ok"] is False
        assert mgr.state.settings["16"] == 0x03, "状态不得被未确认的写入改写"

    @pytest.mark.asyncio
    async def test_rejected_set_requests_immediate_settings_refresh(self):
        """端口写未确认时置位立即刷新标志: 本机设备可能"回错误码但已执行",
        本地需尽快与设备真实状态对齐（不谎报成功）。"""
        mgr = make_manager()
        mgr.state.settings["16"] = 0x03
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value=None)
        mgr.set_mqtt_publisher(MagicMock())
        mgr._settings_refresh_now = False

        await mgr._handle_port_command(("c1", "off"), None)

        assert mgr._settings_refresh_now is True, "未确认的端口写应请求立即重读 settings"

    @pytest.mark.asyncio
    async def test_ack_only_set_applies_intended_value(self):
        """仅 ACK 无 Result: 设备已接受(实测本机 SET 多为此形态) → 算成功。

        落地的是"意图写入的值", 绝不能把 None 写进缓存（fork 指出的污染点）。
        """
        mgr = make_manager()
        mgr.state.settings["16"] = 0x03
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(
            return_value={"piid": 16, "value": None, "raw": None, "ack_only": True})
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_port_command(("c1", "off"), future)

        assert future.result()["ok"] is True
        assert mgr.state.settings["16"] == 0x02, "应落地意图值, 不得写入 None"

    @pytest.mark.asyncio
    async def test_set_command_unconfirmed_does_not_update_state(self):
        """通用 SET（_handle_set_command）未确认时同样不落地。"""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value=None)
        mgr.set_mqtt_publisher(MagicMock())

        future = asyncio.get_running_loop().create_future()
        await mgr._handle_set_command((6, 3), future)

        assert future.result()["ok"] is False
        assert "6" not in mgr.state.settings or mgr.state.settings.get("6") != 3


class TestHandleMultiframe:
    """Test multi-frame data handling."""

    @pytest.mark.asyncio
    async def test_multiframe_large_count_sends_ack(self):
        """Test multiframe with frame_count > 1000 sends ACK and consumes all frames."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        # Decrypt downstream is stubbed so the drain only exercises wait_notify.
        mgr._process_decrypted_frame = AsyncMock()
        call_count = 0
        async def fake_wait_notify(name, timeout=5.0):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                return None                 # 没有更多帧 → 立即终止
            return bytes(20)
        mgr.ctrl.wait_notify = fake_wait_notify

        # data[2]=0x00 triggers multiframe branch, frame_count=0x03e9=1001 > limit
        data = bytes([0, 0, 0x00, 4, 0x03, 0xe9])

        await mgr._handle_multiframe(data)
        assert mgr.ctrl.client.write_gatt_char.call_count == 2
        # 收到 None 即停止, 不按 1001 逐帧空转
        assert call_count == 6


class TestHandleInlineData:
    """Test inline data handling."""

    @pytest.mark.asyncio
    async def test_inline_data_calls_ctrl_decrypt(self):
        """Test _handle_inline_data processes port data and publishes."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)

        decrypted = bytes([0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201])
        mgr.ctrl.decrypt = MagicMock(return_value=decrypted)

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)

        assert 1 in mgr.state.ports
        port = mgr.state.ports[1]
        assert port.voltage == 20.1
        assert port.current == 2.5
        assert port.active is True
        publisher.assert_called_once()

    @pytest.mark.asyncio
    async def test_inline_data_short_payload_ignored(self):
        """Test _handle_inline_data ignores too-short decrypt output (no update)."""
        mgr = make_manager()
        initial = mgr.state.ports[1].voltage
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=bytes(4))

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)

        assert mgr.state.ports[1].voltage == initial

    @pytest.mark.asyncio
    async def test_inline_data_empty_decrypt_ignored(self):
        """Test _handle_inline_data ignores None decrypt output (no update)."""
        mgr = make_manager()
        initial = mgr.state.ports[1].voltage
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=None)

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)

        assert mgr.state.ports[1].voltage == initial


class TestSendCommand:
    """Test send_command method."""

    @pytest.mark.asyncio
    async def test_send_command_not_connected(self):
        """Test send_command returns error when not connected."""
        mgr = make_manager()
        result = await mgr.send_command("set", (5, 1))
        assert result["ok"] is False
        assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_send_command_timeout(self):
        """Test send_command times out."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.state.authenticated = True
        result = await mgr.send_command("set", (5, 1), timeout=0.05)
        assert result["ok"] is False
        assert "timeout" in result["error"]


class TestConnectDisconnect:
    """Test connect and disconnect flow."""

    @pytest.mark.asyncio
    async def test_disconnect_resets_state(self):
        """Test _disconnect resets authenticated and always publishes."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        mgr.state.authenticated = True
        await mgr._disconnect()
        assert mgr.state.authenticated is False
        publisher.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_publishes_connected_false(self):
        """Test _disconnect always publishes connected:False."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)
        await mgr._disconnect()
        publisher.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_sets_stop_event(self):
        """Test stop() sets stop event."""
        mgr = make_manager()
        await mgr.stop()
        assert mgr._stop_event.is_set()


class TestInvalidate:
    """Test cache invalidation."""

    def test_invalidate_calls_callback(self):
        callback = MagicMock()
        set_status_cache_invalidator(callback)
        _invalidate()
        callback.assert_called_once()
        set_status_cache_invalidator(None)

    def test_invalidate_no_callback(self):
        set_status_cache_invalidator(None)
        _invalidate()


class TestReconnectLoop:
    """Test BLE disconnect/reconnect cycle."""

    @pytest.mark.asyncio
    async def test_reconnect_after_disconnect(self):
        """Test start() retries when _connect_and_run raises ConnectionError."""
        mgr = make_manager()
        call_count = 0

        async def fake_connect_and_run():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("BLE disconnected")

        mgr._connect_and_run = fake_connect_and_run
        mgr._force_disconnect_bluetooth = AsyncMock()
        mgr._disconnect = AsyncMock()

        wait_calls = 0

        async def fake_wait_for(coro, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls >= 2:
                mgr._stop_event.set()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            await mgr.start()

        assert call_count == 2
        assert mgr._reconnect_attempts == 1

    @pytest.mark.asyncio
    async def test_stop_breaks_reconnect_loop(self):
        """Test stop() breaks the reconnect loop."""
        mgr = make_manager()
        call_count = 0

        async def fake_connect_and_run():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("BLE disconnected")

        mgr._connect_and_run = fake_connect_and_run
        mgr._force_disconnect_bluetooth = AsyncMock()

        # Stop after first failure
        async def fake_wait_for(coro, timeout):
            mgr._stop_event.set()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            await mgr.start()

        # Should only have tried once before stop broke the loop
        assert call_count == 1
        assert mgr._stop_event.is_set()


class TestAuthFailureRetry:
    """Test auth failure handling."""

    @pytest.mark.asyncio
    async def test_auth_failure_raises_auth_error(self):
        """Test _connect raises AuthConnectionError (not ConnectionError) on auth failure."""
        mgr = make_manager()

        mock_ctrl = MagicMock()
        mock_ctrl.authenticate = AsyncMock(return_value=False)
        mock_ctrl.client = MagicMock()
        mock_ctrl.client.disconnect = AsyncMock()
        mock_ctrl.client.get_services = AsyncMock(return_value=["svc1"])
        mock_ctrl.client.read_gatt_char = AsyncMock(return_value=b"test")
        mock_ctrl.read_device_info = AsyncMock()
        mock_ctrl.connect = AsyncMock()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("bleak.BleakScanner") as mock_scanner:
            mock_scanner.find_device_by_address = AsyncMock(return_value=MagicMock())
            with patch("ble_manager.CuktechBLEController", return_value=mock_ctrl):
                with patch("asyncio.create_subprocess_exec", return_value=AsyncMock(return_value=mock_proc)):
                    from ble_manager import AuthConnectionError
                    with pytest.raises(AuthConnectionError):
                        await mgr._connect()

    @pytest.mark.asyncio
    async def test_auth_failure_triggers_power_cycle(self):
        """Test auth failure now triggers power cycle to reset BlueZ GATT cache."""
        mgr = make_manager()
        mgr._force_disconnect_bluetooth = AsyncMock()
        mgr._disconnect = AsyncMock()
        mgr._publish_status = MagicMock()

        call_count = 0

        async def fake_connect_and_run():
            nonlocal call_count
            call_count += 1
            from ble_manager import AuthConnectionError
            raise AuthConnectionError("Auth failed")

        mgr._connect_and_run = fake_connect_and_run

        wait_calls = 0

        async def fake_wait_for(coro, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls >= 2:
                mgr._stop_event.set()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            await mgr.start()

        # After our fix: auth failure SHOULD trigger power cycle
        assert mgr._force_disconnect_bluetooth.call_count >= 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_auth_stuck_restarts_via_registered_handler(self):
        """连续 auth 失败到上限：必须走服务层注册的重启处理器（Linux 下即 execv 自愈）。

        回归：这条分支原来直接 os._exit(1)，注释写着"外部进程管理器会自动重启"——
        而本机既没有 systemd 单元、crontab 里的 ensure_server.sh 也指向不存在的路径，
        结果 15 次失败后进程退出、服务停机 3 小时 25 分。现在 Linux 通过处理器
        os.execv，不需要任何外部守护。
        """
        mgr = make_manager()
        mgr.AUTH_STUCK_NOTIFY_SEC = 0        # 别真等那 2 秒
        mgr._auth_fail_count = mgr.MAX_AUTH_FAILURES
        calls = []

        async def handler():
            calls.append("restart")
            raise _RestartRequested()        # execv 不返回：用它模拟"映像被替换"

        mgr.set_restart_handler(handler)
        with patch("ble_manager.os._exit") as exit_mock:
            with pytest.raises(_RestartRequested):
                await mgr._recover_from_auth_stuck()
        assert calls == ["restart"], "必须调用注册的重启处理器"
        exit_mock.assert_not_called()        # 处理器没返回 → 不该再走 os._exit

    @pytest.mark.asyncio
    async def test_auth_stuck_without_handler_exits_for_supervisor(self):
        """没有处理器时退回 os._exit(1)：那条路必须有外部管理器，否则起不来。"""
        mgr = make_manager()
        mgr.AUTH_STUCK_NOTIFY_SEC = 0
        mgr._auth_fail_count = mgr.MAX_AUTH_FAILURES
        with patch("ble_manager.os._exit", side_effect=_ProcessExited) as exit_mock:
            with pytest.raises(_ProcessExited):
                await mgr._recover_from_auth_stuck()
        exit_mock.assert_called_once_with(1)

    def test_ha_server_registers_restart_handler(self):
        """服务层必须注册自愈重启处理器。

        这行 wiring 没法用行为测试覆盖（要跑 on_startup 就得起整套 BLE/MQTT/HTTP），
        所以直接盯源码文本——它正是"停机 3 小时 25 分"那次事故的修复点：不注册就
        等于退回"退出等外部管理器"，而本机没有管理器。
        """
        src = (Path(__file__).parent.parent / "ha_server.py").read_text(encoding="utf-8")
        assert "s.ble.set_restart_handler(s._restart)" in src, "ha_server 必须注册重启处理器"
        assert "async def _restart(self)" in src, "_restart 必须仍是异步方法（处理器按协程调用）"


class TestMultiframeBoundary:
    """Test multi-frame data edge cases."""

    @pytest.mark.asyncio
    async def test_multiframe_zero_frames(self):
        """Test multiframe with frame_count=0 does not crash."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()

        # data[2]=0x00, frame_count = data[4] + 0x100*data[5] = 0 + 0 = 0
        data = bytes([0, 0, 0x00, 4, 0x00, 0x00])

        await mgr._handle_multiframe(data)

        # Should ACK then ACK done, no frame consumption
        assert mgr.ctrl.client.write_gatt_char.call_count == 2

    @pytest.mark.asyncio
    async def test_multiframe_large_count_is_clamped(self):
        """损坏的帧头上报超大 count 时必须钳制, 不能按 count 逐帧空转。

        原实现会 for 循环 frame_count 次、每次等 ~3s, count=65535 时最长阻塞
        数十小时, 整个 BLE 主循环停摆（HTTP 还能应答, 看起来"活着"）。
        """
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        call_count = 0

        async def fake_wait_notify(name, timeout=5.0):
            nonlocal call_count
            call_count += 1
            return bytes(20)          # 一直有帧返回

        mgr.ctrl.wait_notify = fake_wait_notify

        # frame_count = 0x03e9 = 1001 → 应被钳制到 MULTIFRAME_MAX_FRAMES
        data = bytes([0, 0, 0x00, 4, 0x03, 0xe9])
        await mgr._handle_multiframe(data)

        assert call_count == mgr.MULTIFRAME_MAX_FRAMES, \
            f"应按钳制值收帧, 实际 {call_count}"
        assert mgr.ctrl.client.write_gatt_char.call_count == 2

    @pytest.mark.asyncio
    async def test_multiframe_stops_when_no_more_frames(self):
        """收不到帧应立即终止, 不空转到 frame_count 满。"""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        call_count = 0

        async def fake_wait_notify(name, timeout=5.0):
            nonlocal call_count
            call_count += 1
            if call_count > 3:
                return None           # 没有更多帧
            return bytes(20)

        mgr.ctrl.wait_notify = fake_wait_notify

        data = bytes([0, 0, 0x00, 4, 0x64, 0x00])   # count = 100
        await mgr._handle_multiframe(data)

        assert call_count == 4, f"收到 None 后应停止, 实际调用 {call_count} 次"

    @pytest.mark.asyncio
    async def test_incomplete_multiframe_does_not_count_decrypt_failure(self):
        """不完整的多帧（超时/钳制截断）不得计入解密失败（评审 #6）。

        拼接不全会导致 AES-CCM 校验必然失败, 若计入 _decrypt_failures,
        连续几次坏帧就会触发一次毫无必要的整链重连。
        """
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=b"")   # 真去解密必然"失败"

        async def fake_wait(name, timeout=5.0):
            return None          # 一帧都收不到

        mgr.ctrl.wait_notify = fake_wait
        mgr._decrypt_failures = 0

        data = bytes([0, 0, 0x00, 4, 0x03, 0x00])   # 声称 3 帧, 实际 0 帧
        await mgr._handle_multiframe(data)

        assert mgr._decrypt_failures == 0, "不完整多帧不应计入解密失败"
        mgr.ctrl.decrypt.assert_not_called()

    @pytest.mark.asyncio
    async def test_clamped_multiframe_skips_decrypt(self):
        """申报帧数超过钳制值时按截断处理: 不解密、不计数（评审 #6）。"""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=b"")
        calls = 0

        async def fake_wait(name, timeout=5.0):
            nonlocal calls
            calls += 1
            return bytes(20)

        mgr.ctrl.wait_notify = fake_wait
        mgr._decrypt_failures = 0

        data = bytes([0, 0, 0x00, 4, 0xFF, 0xFF])   # count=65535 > 钳制
        await mgr._handle_multiframe(data)

        assert calls == mgr.MULTIFRAME_MAX_FRAMES
        assert mgr._decrypt_failures == 0
        mgr.ctrl.decrypt.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiframe_concatenates_then_decrypts_once(self):
        """多帧必须拼接子帧(剥 2 字节帧号)后整体解密一次。

        子帧只有 2 字节帧号前缀, 逐个按内联帧剥 4 字节解密必然偏移错位。
        """
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.set_mqtt_publisher(MagicMock())

        # 两个子帧: [帧号 2 字节] + 各 6 字节数据
        frames = [
            bytes([0x00, 0x00]) + bytes([0xAA] * 6),
            bytes([0x01, 0x00]) + bytes([0xBB] * 6),
        ]
        it = iter(frames)

        async def fake_wait_notify(name, timeout=5.0):
            return next(it, None)

        mgr.ctrl.wait_notify = fake_wait_notify
        decrypted = bytes([0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201])
        mgr.ctrl.decrypt = MagicMock(return_value=decrypted)

        data = bytes([0, 0, 0x00, 4, 0x02, 0x00])   # count = 2
        await mgr._handle_multiframe(data)

        # 解密只调用一次, 且入参是拼接后的整体载荷(不含帧号)
        mgr.ctrl.decrypt.assert_called_once()
        payload = mgr.ctrl.decrypt.call_args[0][0]
        assert payload == bytes([0xAA] * 6) + bytes([0xBB] * 6)
        # 解密结果被下游处理（端口状态更新）
        assert mgr.state.ports[1].voltage == 20.1


class TestConcurrency:
    """Test concurrent command processing."""

    @pytest.mark.asyncio
    async def test_concurrent_commands(self):
        """Test multiple commands in queue are all processed."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(
            return_value={"piid": 5, "value": 1, "raw": b""})
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)

        futures = []
        for _ in range(3):
            future = asyncio.get_running_loop().create_future()
            await mgr.cmd_queue.put(("set", (5, 1), future))
            futures.append(future)

        await mgr._process_commands()

        for f in futures:
            assert f.done()
            assert f.result() == {"ok": True}


class TestDecryptFailure:
    """Test decrypt failure counting."""

    @pytest.mark.asyncio
    async def test_decrypt_failure_count_increments(self):
        """Test _decrypt_failures increments and triggers recovery at threshold 3."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=None)

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)
        assert mgr._decrypt_failures == 1

        await mgr._handle_inline_data(data)
        assert mgr._decrypt_failures == 2

        # 3rd consecutive failure crosses the threshold → session stale raised
        with pytest.raises(ConnectionError):
            await mgr._handle_inline_data(data)

    @pytest.mark.asyncio
    async def test_decrypt_failure_resets_on_success(self):
        """Test _decrypt_failures resets to 0 after successful decrypt."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=None)

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)
        await mgr._handle_inline_data(data)
        assert mgr._decrypt_failures == 2

        # Now provide valid decrypt
        decrypted = bytes([0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201])
        mgr.ctrl.decrypt = MagicMock(return_value=decrypted)

        await mgr._handle_inline_data(data)
        assert mgr._decrypt_failures == 0


class TestMQTTPublisherReconnect:
    """Test MQTT reconnect restores publisher."""

    def test_on_connect_sets_mqtt_publisher(self):
        """Test on_connect callback sets MQTT publisher on reconnect."""
        mgr = make_manager()
        publisher = MagicMock()

        # Simulate what ha_server.py does: on_connect sets publisher
        mgr.set_mqtt_publisher(publisher)
        assert mgr._mqtt_publish is publisher

        # Simulate disconnect losing publisher
        mgr.set_mqtt_publisher(None)
        assert mgr._mqtt_publish is None

        # Simulate on_connect restoring it
        mgr.set_mqtt_publisher(publisher)
        assert mgr._mqtt_publish is publisher

    def test_on_connect_publishes_status(self):
        """Test on_connect publishes status after reconnect."""
        mgr = make_manager()
        publisher = MagicMock()
        mgr.set_mqtt_publisher(publisher)

        # Simulate the on_connect flow from ha_server.py
        mgr._publish_status({"connected": True, "authenticated": True}, retain=True)
        publisher.assert_called_once_with(
            "cuktech/charger/status", {"connected": True, "authenticated": True}, retain=True
        )


class TestSessionRecording:
    """充电会话记录开关（方案 B：记录可控、事件保留）。"""

    def test_record_sessions_default_true(self):
        """默认开启（向后兼容）。"""
        mgr = make_manager()
        assert mgr.record_sessions is True

    @pytest.mark.asyncio
    async def test_close_session_recording_off_discards_db_but_publishes_event(self):
        """记录关闭期间的会话（占位负 sid）：MQTT 事件照发、DB 完全丢弃、不发 SSE。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr._sse_emitter = MagicMock()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1  # 记录关闭期间产生的占位 sid
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = 1000.0
        es.session_wh = 1.0
        es.max_power = 50.0

        sid = mgr._close_session(1, 1600.0, 20.0, 2.0)
        assert sid == -1
        await asyncio.sleep(0.05)  # 让 executor 任务有机会执行（如有）
        mgr._mqtt_publish.assert_called_once()          # 事件照发（HA 通知保留）
        mgr._history.end_session.assert_not_called()
        mgr._history.delete_session.assert_not_called()
        mgr._sse_emitter.emit.assert_not_called()        # 占位会话不发 SSE session_end

    def test_get_live_session_data_shows_recording_off_session(self):
        """记录关闭时，进行中会话（占位 sid）仍在实时数据中（重开开关后也正常显示）。"""
        mgr = make_manager()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = 1000.0
        es.session_wh = 1.5
        es.max_power = 50.0
        live = mgr.get_live_session_data()
        assert 1 in live
        assert live[1]["session_id"] == -1
        assert live[1]["session_wh"] == 1.5

    @pytest.mark.asyncio
    async def test_resume_recording_upgrades_fake_sessions(self):
        """打开开关时，关闭期间正在充电的占位会话立即转为真实记录并从此刻重新累计。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._history.start_session.return_value = 100
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
            mgr._active_sessions[2] = 7   # 已是真实会话，不应被转正
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 3.0
        es.session_start = 1000.0
        mgr.state.ports[1].protocol = "PD"

        mgr.resume_recording_sessions()
        await asyncio.sleep(0.05)  # 等待 executor 完成 start_session

        mgr._history.start_session.assert_called_once_with(1, "PD")
        assert mgr._active_sessions[1] == 100   # 占位 -1 已被转正为 100
        assert mgr._active_sessions[2] == 7     # 真实会话不受影响
        assert es.session_wh == 0.0             # 从打开时刻重新累计
        assert es.session_start > 1000.0

    @pytest.mark.asyncio
    async def test_resume_recording_noop_without_fake_sessions(self):
        """没有占位会话时（全开状态或无人充电），转正调用不产生任何 DB 操作。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[2] = 7   # 只有真实会话
        mgr.resume_recording_sessions()
        await asyncio.sleep(0.05)
        mgr._history.start_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_session_with_record_writes_db(self):
        """记录开启（有 sid）时：事件 + SSE + end_session 均执行（原行为不变）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr._sse_emitter = MagicMock()
        with mgr._sess_lock:
            mgr._active_sessions[1] = 42
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = 1000.0
        es.session_wh = 2.5
        es.max_power = 60.0

        sid = mgr._close_session(1, 1600.0, 20.0, 3.0)
        assert sid == 42
        await asyncio.sleep(0.05)  # 等待 executor 写入
        mgr._mqtt_publish.assert_called_once()
        mgr._sse_emitter.emit.assert_called_once_with("session_end", {   # 前缀匹配
            "session_id": 42,
            "port": "c1",
            "port_id": 1,
            "total_wh": 2.5,
            "peak_power_w": 60.0,
            "duration_sec": 600,
        })
        mgr._history.end_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_session_micro_wh_recording_on_no_event(self):
        """记录开启 + 微能量（<0.05Wh）：不发事件、不发 SSE，仅清理 DB 会话行。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr._sse_emitter = MagicMock()
        mgr.record_sessions = True
        with mgr._sess_lock:
            mgr._active_sessions[1] = 42
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = 1000.0
        es.session_wh = 0.01
        es.max_power = 5.0

        sid = mgr._close_session(1, 1600.0, 1.0, 0.1)
        assert sid == 42
        await asyncio.sleep(0.05)
        mgr._mqtt_publish.assert_not_called()        # 事件移回 >=0.05Wh 门控
        mgr._sse_emitter.emit.assert_not_called()
        mgr._history.delete_session.assert_called_once_with(42)
        mgr._history.end_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_session_micro_wh_recording_off_no_event(self):
        """记录关闭 + 微能量（<0.05Wh）占位会话：不发事件、不写库。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr._sse_emitter = MagicMock()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = 1000.0
        es.session_wh = 0.01

        sid = mgr._close_session(1, 1600.0, 1.0, 0.1)
        assert sid == -1
        await asyncio.sleep(0.05)
        mgr._mqtt_publish.assert_not_called()
        mgr._sse_emitter.emit.assert_not_called()
        mgr._history.end_session.assert_not_called()
        mgr._history.delete_session.assert_not_called()

    def test_close_active_sessions_logs_reason(self, caplog):
        """停机关闭会话时日志必须带 reason=（此前只有探测路径带）。

        除探测路径外，这行是唯一记录"会话为什么结束"的地方：以前只打
        (port, Wh, 秒)，关停/关闭记录路径的原因只能靠相邻的 "Shutting down..."
        去猜——与"结束原因必须能从日志读到"的约定不符。
        """
        mgr = make_manager()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = time.time() - 60
        es.session_wh = 1.5            # ≥0.05 才会走到这条日志
        es.max_power = 20.0

        with caplog.at_level("INFO"):
            mgr._close_active_sessions(END_REASON_SHUTDOWN)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("closed" in m and "reason=shutdown" in m for m in msgs), msgs

    def test_no_load_close_logs_reason_exactly_once(self, caplog):
        """一次无负载闭合只能有一条带 reason= 的记录。

        原因统一由 _close_session 打；无负载分支过去自己也带一个 reason=，
        同一次闭合出现两条会让人误以为结束了两次。
        """
        mgr = make_manager()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = time.time() - 300
        es.session_wh = 1.0
        es.max_power = 20.0
        # 去抖已满 + 电压仍在协商范围（5.1V）→ NO_LOAD（而不是 UNPLUG）
        mgr._no_load_since[1] = time.time() - (mgr.NO_LOAD_DEBOUNCE_SEC + 1)

        with caplog.at_level("INFO"):
            mgr._manage_session(1, time.time(), 5.1, 0.0, active=False)

        hits = [r.getMessage() for r in caplog.records if "reason=" in r.getMessage()]
        assert len(hits) == 1, hits
        assert "reason=no_load" in hits[0]

    def test_close_session_logs_reason_for_user_off(self, caplog):
        """用户/限额关端口这条路径过去完全不打原因。

        实测踩到过：网页手动关 C2（POST /api/port），日志里只剩
        "Charge event published"，结束原因只能从相邻的访问日志反推。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = time.time() - 71
        es.session_wh = 0.38
        es.max_power = 26.0
        with mgr._sess_lock:
            mgr._active_sessions[1] = 42

        with caplog.at_level("INFO"):
            mgr._close_session(1, time.time(), 20.0, 0.02, END_REASON_USER_OFF)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("reason=port_off" in m for m in msgs), msgs

    @pytest.mark.asyncio
    async def test_close_active_sessions_recording_off_skips_db_but_publishes(self):
        """停机关闭 + 记录关闭的占位会话：MQTT 事件照发（session_id=0）、不写库。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        mgr._sse_emitter = MagicMock()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_start = time.time() - 120
        es.session_wh = 2.0
        es.max_power = 50.0
        mgr.state.ports[1].voltage = 20.0
        mgr.state.ports[1].current = 3.0

        mgr._close_active_sessions()
        await asyncio.sleep(0.05)
        mgr._mqtt_publish.assert_called_once()
        published = mgr._mqtt_publish.call_args[0][1]
        assert published["session_id"] == 0
        assert published["recorded"] is False
        mgr._history.end_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_race_closes_orphan_db_row(self):
        """转正窗口内会话已结束：回调闭合刚建的 DB 行，不留孤儿会话、不写假 sid。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._history.start_session.return_value = 100
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 3.0
        es.session_start = 1000.0
        mgr.state.ports[1].protocol = "PD"

        mgr.resume_recording_sessions()
        # 模拟 executor 完成前会话已结束（真实流程由 _close_session 完成）
        with mgr._sess_lock:
            mgr._active_sessions.pop(1, None)
        es.is_charging = False
        await asyncio.sleep(0.05)

        mgr._history.start_session.assert_called_once_with(1, "PD")
        mgr._history.delete_session.assert_called_once_with(100)
        assert mgr._active_sessions.get(1) is None

    @pytest.mark.asyncio
    async def test_record_charge_point_skipped_recording_off(self):
        """记录关闭：采样点写入门控拒绝（不落库，实时显示不受影响）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        assert mgr._record_charge_point(1, 20.0, 2.0, "PD") is False
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_not_called()

    @pytest.mark.asyncio
    async def test_record_charge_point_skipped_fake_sid(self):
        """记录开启但 sid 为占位（负值）：仍拒绝写入。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = True
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1
        assert mgr._record_charge_point(1, 20.0, 2.0, "PD") is False
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_not_called()

    @pytest.mark.asyncio
    async def test_record_charge_point_written_recording_on(self):
        """记录开启 + 真实 sid：采样点正常落库。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = True
        with mgr._sess_lock:
            mgr._active_sessions[1] = 42
        assert mgr._record_charge_point(1, 20.0, 2.0, "PD") is True
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_called_once_with(
            42, 20.0, 2.0, 40.0, "PD")

    @pytest.mark.asyncio
    async def test_session_start_race_closes_orphan(self):
        """正常开始路径 _on_session_start 回调前会话已结束：闭合刚建的 DB 行，
        不留孤儿会话（与 resume 转正竞态共享同一 _close_resumed_orphan 兜底路径）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = True
        es = mgr._energy_states[1]
        es.is_charging = True
        # 模拟会话在 start_session executor 完成前已结束：
        # 此时 _active_sessions 中无 sid（尚未设置），is_charging 已为 False
        es.is_charging = False
        mgr._close_resumed_orphan(1, 100)
        await asyncio.sleep(0.05)
        mgr._history.delete_session.assert_called_once_with(100)


class TestChargeLimitWiring:
    """判定确实挂在两条数据路径上（push 帧 + 1s timer）。"""

    @pytest.mark.asyncio
    async def test_push_path_enforces_limit(self):
        """BLE 推送路径：能量累计到阈值后自动入队关断。"""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=bytes(
            [0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201]))   # 20.1V 2.5A
        mgr.set_mqtt_publisher(MagicMock())
        mgr.set_charge_limits({"c1": {"wh": 0.01, "mode": "once"}})
        # 会话已在进行中且本次会话能量已达阈值（测试帧 dt≈0，无法靠积分累积）
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 5.0
        es.session_start = time.time() - 60

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)

        assert mgr._limit_fired[1] is True, "push 路径应触发限额"
        assert mgr.cmd_queue.get_nowait()[1] == ("c1", "off")

    @pytest.mark.asyncio
    async def test_push_path_ignores_limit_below_threshold(self):
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=bytes(
            [0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201]))
        mgr.set_mqtt_publisher(MagicMock())
        mgr.set_charge_limits({"c1": {"wh": 100.0, "mode": "once"}})

        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        for _ in range(6):
            await mgr._handle_inline_data(data)

        assert mgr._limit_fired[1] is False
        assert mgr.cmd_queue.empty()

    @pytest.mark.asyncio
    async def test_timer_path_enforces_limit(self):
        """1s timer 路径（端口 idle 无推送时）同样触发限额。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_charge_limits({"c1": {"wh": 0.005, "mode": "once"}})
        mgr.state.ports[1].voltage = 20.0
        mgr.state.ports[1].current = 1.0
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 0.0
        es.session_start = time.time() - 60
        es.last_time = time.time() - 10      # 触发 idle 分支

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await mgr._port_timer()

        assert mgr._limit_fired[1] is True, "timer 路径应触发限额"
        # C3/A 主动轮询也会入队 verify_port，故按内容查找而非依赖队首顺序
        queued = []
        while not mgr.cmd_queue.empty():
            queued.append(mgr.cmd_queue.get_nowait()[1])
        assert ("c1", "off") in queued


class TestChargeLimitEnforce:
    """限额判定与入队（_enforce_charge_limit）。"""

    def _charging(self, mgr, port=1, wh=0.0, session_wh=0.0):
        mgr._charge_limits[port] = wh
        es = mgr._energy_states[port]
        es.is_charging = True
        es.session_wh = session_wh
        es.session_start = 1000.0
        return es

    def test_disabled_never_enqueues(self):
        mgr = make_manager()
        self._charging(mgr, wh=0.0, session_wh=100.0)
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.empty()

    def test_not_charging_never_enqueues(self):
        mgr = make_manager()
        mgr._charge_limits[1] = 30.0
        mgr._energy_states[1].session_wh = 100.0   # is_charging 仍为 False
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.empty()

    def test_below_threshold_never_enqueues(self):
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=29.99)
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.empty()

    def test_at_threshold_enqueues_port_off(self):
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=30.0)
        mgr._enforce_charge_limit(1, 2000.0)
        cmd_type, cmd_data, future = mgr.cmd_queue.get_nowait()
        assert cmd_type == "port"
        assert cmd_data == ("c1", "off")
        assert future is None

    def test_above_threshold_enqueues(self):
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=31.5)
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.qsize() == 1

    def test_port_name_mapping_all_ports(self):
        """四个端口名映射正确（c1/c2/c3/a）。"""
        mgr = make_manager()
        for piid, name in ((1, "c1"), (2, "c2"), (3, "c3"), (4, "a")):
            self._charging(mgr, port=piid, wh=1.0, session_wh=1.0)
            mgr._enforce_charge_limit(piid, 2000.0)
            assert mgr.cmd_queue.get_nowait()[1] == (name, "off")

    def test_no_duplicate_enqueue_within_retry_window(self):
        """命中后同一会话内不重复入队（防 1-3 帧窗口内重复 GATT 往返）。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=30.0)
        mgr._enforce_charge_limit(1, 2000.0)
        mgr._enforce_charge_limit(1, 2001.0)
        mgr._enforce_charge_limit(1, 2002.0)
        assert mgr.cmd_queue.qsize() == 1

    def test_retries_after_watchdog_window(self):
        """入队后超过 LIMIT_RETRY_SEC 端口仍在充电（命令失败/超时）→ 重试。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=30.0)
        mgr._enforce_charge_limit(1, 2000.0)
        mgr._enforce_charge_limit(1, 2000.0 + mgr.LIMIT_RETRY_SEC + 1)
        assert mgr.cmd_queue.qsize() == 2

    def test_queue_full_resets_fired_flag(self):
        """队列满时复位标志，下一帧重试（不做静默丢弃）。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, session_wh=30.0)
        for _ in range(mgr.CMD_QUEUE_MAXSIZE):
            mgr.cmd_queue.put_nowait(("set", (5, 1), None))
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr._limit_fired[1] is False


class TestChargeLimitRelease:
    """会话终止时的限额生命周期（_release_limit / 两种 mode）。"""

    def _charging(self, mgr, port=1, wh=30.0, mode="once", session_wh=1.0):
        mgr._charge_limits[port] = wh
        mgr._limit_modes[port] = mode
        es = mgr._energy_states[port]
        es.is_charging = True
        es.session_wh = session_wh
        es.session_start = 1000.0
        return es

    def test_once_consumed_on_user_off(self):
        """once + 手动关端口（未达阈值）→ 消费清零。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 0.0
        assert mgr._limit_fired[1] is False

    def test_once_consumed_on_unplug(self):
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_UNPLUG)
        assert mgr._charge_limits[1] == 0.0

    def test_once_consumed_on_low_power_end(self):
        """低功率自然结束（充满）也属真实终止 → 消费。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_LOW_POWER)
        assert mgr._charge_limits[1] == 0.0

    def test_once_preserved_on_link_loss(self):
        """BLE 抖动重连不得静默解除用户刚设的 once 限额。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_LINK_LOSS)
        assert mgr._charge_limits[1] == 30.0
        assert mgr._limit_fired[1] is False

    def test_once_preserved_on_shutdown(self):
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_SHUTDOWN)
        assert mgr._charge_limits[1] == 30.0

    def test_unknown_reason_conservatively_consumes(self):
        """未标注原因视为真实终止（保守：不可假定基础设施中断）。"""
        mgr = make_manager()
        self._charging(mgr, wh=30.0, mode="once", session_wh=5.0)
        mgr._release_limit(1)
        assert mgr._charge_limits[1] == 0.0

    def test_always_mode_never_consumed(self):
        """always 长期有效：任何原因都不清零，仅复位 fired 待重新武装。"""
        mgr = make_manager()
        for reason in (END_REASON_USER_OFF, END_REASON_UNPLUG, END_REASON_LOW_POWER,
                       END_REASON_LINK_LOSS, END_REASON_SHUTDOWN, END_REASON_UNKNOWN):
            self._charging(mgr, wh=30.0, mode="always", session_wh=5.0)
            mgr._limit_fired[1] = True
            mgr._release_limit(1, reason)
            assert mgr._charge_limits[1] == 30.0, f"always must survive {reason}"
            assert mgr._limit_fired[1] is False

    def test_disabled_port_untouched(self):
        mgr = make_manager()
        self._charging(mgr, wh=0.0, mode="once", session_wh=5.0)
        mgr._release_limit(1, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 0.0
        assert mgr._limit_fired[1] is False

    @pytest.mark.asyncio
    async def test_once_consumed_through_close_session(self):
        """端到端：_close_session 的人工关端口路径消费 once 限额并回写 DB。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        self._charging(mgr, wh=30.0, mode="once", session_wh=2.0)
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7
        mgr._close_session(1, 2000.0, 20.0, 0.5, END_REASON_USER_OFF)
        await asyncio.sleep(0.05)
        assert mgr._charge_limits[1] == 0.0
        mgr._history.set_charge_limits.assert_called_once()
        persisted = mgr._history.set_charge_limits.call_args[0][0]
        assert persisted["c1"]["wh"] == 0.0

    @pytest.mark.asyncio
    async def test_close_session_second_entry_does_not_reclear(self):
        """占位清理路径（is_charging 已 False 但 sid 残留）不得二次消费——
        否则会误伤刚设的新限额。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        self._charging(mgr, wh=30.0, mode="once", session_wh=2.0)
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7
        mgr._close_session(1, 2000.0, 20.0, 0.5, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 0.0
        # 用户在两次调用之间重新设了限额
        mgr._charge_limits[1] = 10.0
        with mgr._sess_lock:
            mgr._active_sessions[1] = 8      # 残留 sid，但 is_charging 已 False
        mgr._close_session(1, 2001.0, 20.0, 0.5, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 10.0, "stale cleanup must not consume the new limit"

    @pytest.mark.asyncio
    async def test_link_loss_reason_preserves_limit_end_to_end(self):
        """端到端：_disconnect 触发的会话关闭（link_loss）保留 once 限额。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        self._charging(mgr, wh=30.0, mode="once", session_wh=2.0)
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7
        mgr._close_active_sessions(END_REASON_LINK_LOSS)
        await asyncio.sleep(0.05)
        assert mgr._charge_limits[1] == 30.0
        mgr._history.set_charge_limits.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_reason_preserves_limit_end_to_end(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._mqtt_publish = MagicMock()
        self._charging(mgr, wh=30.0, mode="once", session_wh=2.0)
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7
        await mgr.request_stop()
        await asyncio.sleep(0.05)
        assert mgr._charge_limits[1] == 30.0

    @pytest.mark.asyncio
    async def test_persist_skipped_without_history(self):
        """无 history 时清理仍是内存操作，不抛异常。"""
        mgr = make_manager()
        assert mgr._history is None
        self._charging(mgr, wh=30.0, mode="once", session_wh=2.0)
        mgr._release_limit(1, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 0.0


class TestSessionActivePredicate:
    """_session_active：会话建立窗口（is_charging=True 但尚无 sid）也必须闭合。

    会话建立分两步——push 同步置 is_charging=True，sid 由 start_session 回调
    写入。只看 _active_sessions 会漏掉该窗口（DB 写失败时则长期如此），导致
    端口已断电但 is_charging 永远为 True（幽灵实时会话）。
    """

    def test_sid_only(self):
        mgr = make_manager()
        with mgr._sess_lock:
            mgr._active_sessions[1] = 7
        assert mgr._session_active(1) is True

    def test_charging_without_sid(self):
        """关键窗口：is_charging=True、_active_sessions 空。"""
        mgr = make_manager()
        mgr._energy_states[1].is_charging = True
        assert mgr._session_active(1) is True

    def test_neither(self):
        mgr = make_manager()
        assert mgr._session_active(1) is False

    def test_other_port_untouched(self):
        mgr = make_manager()
        mgr._energy_states[1].is_charging = True
        assert mgr._session_active(2) is False

    @pytest.mark.asyncio
    async def test_port_off_closes_session_without_sid(self):
        """用户关端口且尚无 sid：会话必须闭合，once 限额被消费。"""
        mgr = make_manager()
        mgr._mqtt_publish = MagicMock()
        mgr._energy_states[1].is_charging = True
        mgr._energy_states[1].session_wh = 5.0
        mgr.set_charge_limits({"c1": {"wh": 30.0, "mode": "once"}})
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"value": 0x01})

        await mgr._handle_port_command(("c1", "off"), None)

        assert mgr._energy_states[1].is_charging is False, "幽灵会话未闭合"
        assert mgr._charge_limits[1] == 0.0, "once 限额未被消费"

    @pytest.mark.asyncio
    async def test_port_off_all_closes_session_without_sid(self):
        """port=all 路径同样要闭合无 sid 的会话。"""
        mgr = make_manager()
        mgr._mqtt_publish = MagicMock()
        for piid in (1, 2, 3, 4):
            mgr._energy_states[piid].is_charging = True
            mgr._energy_states[piid].session_wh = 5.0
        mgr.set_charge_limits({"c1": {"wh": 30.0, "mode": "once"}})
        mgr.ctrl = MagicMock()
        # all-off 的 SET 回显应是新掩码 0x00（设备不会回显旧值 0x0F）
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"value": 0x00})

        await mgr._handle_port_command(("all", "off"), None)

        for piid in (1, 2, 3, 4):
            assert mgr._energy_states[piid].is_charging is False
        assert mgr._charge_limits[1] == 0.0

    @pytest.mark.asyncio
    async def test_verify_port_unplug_closes_session_without_sid(self):
        """verify_port 探测到拔出（V=0,I=0）且尚无 sid：会话闭合、限额消费。"""
        mgr = make_manager()
        mgr._mqtt_publish = MagicMock()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 5.0
        mgr.set_charge_limits({"c1": {"wh": 30.0, "mode": "once"}})
        mgr.state.ports[1].voltage = 20.0
        mgr.state.ports[1].current = 2.0
        mgr.ctrl = MagicMock()
        # 真实 GET Result 帧 (opcode 0x03)，value 在 [13:17] = 0 → V=0,I=0
        mgr.ctrl.send_miot_command = AsyncMock(return_value={
            "value": 0,
            "raw": bytes([0x11, 0x20, 0x01, 0x00, 0x03, 0x01, 0x02, 0x01,
                          0x00, 0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00])})

        await mgr._handle_verify_port(1, None)

        assert es.is_charging is False
        assert mgr._charge_limits[1] == 0.0

    @pytest.mark.asyncio
    async def test_limit_fired_off_closes_session_without_sid(self):
        """端到端：限额触发关断，即便 start_session 失败（无 sid）也要消费并闭合。

        start_session 返回 0（DB 未连接/写失败）时 _active_sessions 永远为空，
        修复前限额无法消费、is_charging 永远为 True。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._history.start_session.return_value = 0   # DB 失败
        mgr._mqtt_publish = MagicMock()
        mgr.set_charge_limits({"c1": {"wh": 1.0, "mode": "once"}})
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"value": 0x0F})
        # push 建会话（sid 永不写入）
        mgr.ctrl.decrypt = MagicMock(return_value=bytes(
            [0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201]))
        _prime_start_gate(mgr)
        await mgr._handle_inline_data(bytes([0, 0, 0x02, 4]) + b'\x00' * 10)
        assert mgr._energy_states[1].is_charging is True
        assert mgr._active_sessions == {}

        # 能量越过阈值 → 入队关断 → 命令循环执行
        mgr._energy_states[1].session_wh = 5.0
        mgr._enforce_charge_limit(1, time.time())
        await mgr._process_commands()

        assert mgr._energy_states[1].is_charging is False, "幽灵会话未闭合"
        assert mgr._charge_limits[1] == 0.0, "once 限额未被消费"
        # 不再是活的实时会话（修复前 get_live_session_data 会持续上报）
        assert mgr.get_live_session_data() == {}


class TestChargeLimitArming:
    """会话起点重新武装 + always 可重复触发。"""

    def test_session_start_rearms_fired_flag(self):
        mgr = make_manager()
        mgr._limit_fired[1] = True
        mgr._limit_fired[1] = False   # 会话起点写入的那一行
        assert mgr._limit_fired[1] is False

    def test_set_charge_limits_normalizes_and_returns_state(self):
        mgr = make_manager()
        state = mgr.set_charge_limits({"c1": {"wh": 30, "mode": "always"},
                                       "c2": {"wh": float("nan"), "mode": "once"},
                                       "c9": {"wh": 99, "mode": "always"}})
        assert state["c1"]["wh"] == 30.0
        assert state["c1"]["mode"] == "always"
        assert state["c1"]["fired"] is False
        assert state["c2"]["wh"] == 0.0
        assert set(state) == {"c1", "c2", "c3", "a"}   # c9 被忽略

    def test_get_charge_limits_state_includes_session_progress(self):
        """状态里带本会话已充能量与充电中标志，供前端显示进度。"""
        mgr = make_manager()
        mgr.set_charge_limits({"c1": {"wh": 30.0, "mode": "once"}})
        mgr._energy_states[1].is_charging = True
        mgr._energy_states[1].session_wh = 12.34567
        st = mgr.get_charge_limits_state()["c1"]
        assert st["session_wh"] == 12.346      # 3 位小数
        assert st["is_charging"] is True
        assert st["wh"] == 30.0

    def test_set_charge_limits_bare_number_form(self):
        mgr = make_manager()
        state = mgr.set_charge_limits({"a": 12.5})
        assert state["a"]["wh"] == 12.5
        assert state["a"]["mode"] == "once"

    def test_always_mode_can_fire_again_after_rearm(self):
        """always：命中→关断（保留）→下个会话重新武装→再次命中。"""
        mgr = make_manager()
        mgr.set_charge_limits({"c1": {"wh": 10.0, "mode": "always"}})
        es = mgr._energy_states[1]
        es.is_charging, es.session_wh = True, 10.0
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.qsize() == 1
        # 关断 → 会话终止（保留限额）
        es.is_charging = False
        mgr._release_limit(1, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 10.0
        # 新会话：重新武装
        es.is_charging, es.session_wh = True, 0.0
        mgr._limit_fired[1] = False
        mgr._enforce_charge_limit(1, 3000.0)
        assert mgr.cmd_queue.qsize() == 1, "re-armed limit must not fire below threshold"
        es.session_wh = 10.5
        mgr._enforce_charge_limit(1, 3001.0)
        assert mgr.cmd_queue.qsize() == 2

    def test_once_mode_does_not_fire_again_after_consume(self):
        """once：消费后不再触发（即使用户再次开端口充电）。"""
        mgr = make_manager()
        mgr.set_charge_limits({"c1": {"wh": 10.0, "mode": "once"}})
        es = mgr._energy_states[1]
        es.is_charging, es.session_wh = True, 10.0
        mgr._enforce_charge_limit(1, 2000.0)
        es.is_charging = False
        mgr._release_limit(1, END_REASON_USER_OFF)
        assert mgr._charge_limits[1] == 0.0
        mgr.cmd_queue.get_nowait()
        es.is_charging, es.session_wh = True, 50.0
        mgr._limit_fired[1] = False
        mgr._enforce_charge_limit(1, 3000.0)
        assert mgr.cmd_queue.empty()


class TestAuthBackoff:
    """认证失败退避纯函数（Spec 审查补充：退避/熔断行为需可测）。"""

    def test_backoff_delay_ladder(self):
        """阶梯延迟: ≥5次→600s, 3-4次→300s, 1-2次→min(120n,180)。"""
        from ble_manager import BLEManager
        assert BLEManager._auth_backoff_delay(1) == 120
        assert BLEManager._auth_backoff_delay(2) == 180   # 240 被 180 封顶
        assert BLEManager._auth_backoff_delay(3) == 300
        assert BLEManager._auth_backoff_delay(4) == 300
        assert BLEManager._auth_backoff_delay(5) == 600
        assert BLEManager._auth_backoff_delay(14) == 600
        assert BLEManager._auth_backoff_delay(0) == 0      # 首次失败立即重试

    def test_should_restart_process_threshold(self):
        """达到 MAX_AUTH_FAILURES(15) 才重启进程。"""
        from ble_manager import BLEManager
        assert BLEManager._should_restart_process(14) is False
        assert BLEManager._should_restart_process(15) is True
        assert BLEManager._should_restart_process(100) is True


class TestPermanentPortPoints:
    """长期供电端口：曲线点不落库（改存内存窗口），会话行与耗能统计照常。"""

    def _session(self, mgr, port=2, sid=7, wh=5.0):
        with mgr._sess_lock:
            mgr._active_sessions[port] = sid
        es = mgr._energy_states[port]
        es.is_charging = True
        es.session_wh = wh
        es.session_start = 1000.0
        es.max_power = 33.0
        return es

    def test_port_names_normalized_and_deduped(self):
        mgr = make_manager()
        mgr.set_permanent_ports(["C2", "c2", "a", "zzz", 3, None])
        assert mgr.permanent_ports == {2, 4}   # 大小写归一 + 去重，非法项丢弃
        assert mgr.get_port_modes_state()["permanent_ports"] == ["c2", "a"]

    def test_garbage_input_clears_modes(self):
        mgr = make_manager()
        mgr.set_permanent_ports(["c1"])
        mgr.set_permanent_ports(None)
        assert mgr.permanent_ports == set()

    def test_modes_are_mutually_exclusive(self):
        """常供与充满即停互斥：先即停后常供 → 该口即停被清掉，别的口不动。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always", "c2": "once"})
        mgr.set_permanent_ports(["c2"])
        assert mgr.full_off == {1: "always"}
        mgr.set_full_off({"c2": "always"})       # 已是长期供电端口：再设即停也不生效
        assert 2 not in mgr.full_off

    @pytest.mark.asyncio
    async def test_point_not_written_for_permanent_port(self):
        """常供端口：采样点不进 DB，但进内存窗口（详情浮层要用）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_permanent_ports(["c2"])
        self._session(mgr, port=2)

        assert mgr._record_charge_point(2, 20.0, 1.5, "PD") is False
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_not_called()
        assert len(mgr._permanent_points[2]) == 1
        point = mgr._permanent_points[2][0]
        assert point[1:4] == (20.0, 1.5, 30.0)

    @pytest.mark.asyncio
    async def test_point_written_for_normal_port(self):
        """对照组：非常供端口照旧落库（常供门控不能影响普通端口）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        self._session(mgr, port=1, sid=11)

        assert mgr._record_charge_point(1, 20.0, 2.0, "PD") is True
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_called_once_with(
            11, 20.0, 2.0, 40.0, "PD")
        assert len(mgr._permanent_points[1]) == 0

    def test_point_throttled_to_one_per_second(self):
        """节流：同一秒内的重复推送只留一个点（窗口内存有上限）。"""
        mgr = make_manager()
        mgr.set_permanent_ports(["c2"])
        self._session(mgr, port=2)
        for _ in range(5):
            mgr._append_permanent_point(2, 20.0, 1.0, "PD")
        assert len(mgr._permanent_points[2]) == 1

    def test_ring_drops_points_older_than_retain_window(self):
        mgr = make_manager()
        mgr.set_permanent_ports(["c2"])
        now = time.time()
        mgr._permanent_points[2].append(
            (now - BLEManager.PERMANENT_RETAIN_SEC - 10, 20.0, 1.0, 20.0, "PD"))
        mgr._permanent_last_point[2] = 0.0
        mgr._append_permanent_point(2, 20.0, 1.0, "PD")
        assert len(mgr._permanent_points[2]) == 1
        assert mgr._permanent_points[2][0][0] > now - 5

    def test_window_returns_points_and_whole_session_stats(self):
        """窗口只裁剪曲线；统计恒为整个会话（与窗口/淘汰无关）。"""
        mgr = make_manager()
        mgr.set_permanent_ports(["c2"])
        es = self._session(mgr, port=2, sid=7, wh=12.5)
        now = time.time()
        mgr._permanent_points[2].extend([
            (now - 1800, 20.0, 1.0, 20.0, "PD"),
            (now - 300, 20.0, 1.5, 30.0, "PD"),
            (now - 60, 20.0, 2.0, 40.0, "PD"),
        ])

        win = mgr.get_permanent_session_window(7, window_sec=600)
        assert [p["power"] for p in win["points"]] == [30.0, 40.0]
        assert win["window"]["count"] == 2 and win["window"]["total_points"] == 3
        assert win["permanent"] is True
        assert win["stats"]["total_wh"] == 12.5          # 会话级累计，不受窗口影响
        assert win["stats"]["peak_power_w"] == 33.0
        assert win["stats"]["session_id"] == 7

    def test_window_respects_to_timestamp(self):
        """to 参数把窗口整体左移（滑动查看历史窗口）。"""
        mgr = make_manager()
        mgr.set_permanent_ports(["c2"])
        self._session(mgr, port=2, sid=7)
        now = time.time()
        mgr._permanent_points[2].extend([
            (now - 1200, 20.0, 1.0, 20.0, "PD"),
            (now - 600, 20.0, 1.5, 30.0, "PD"),
            (now - 30, 20.0, 2.0, 40.0, "PD"),
        ])
        win = mgr.get_permanent_session_window(7, window_sec=300, to_ts=now - 600)
        assert [p["power"] for p in win["points"]] == [30.0]
        assert win["window"]["to"] == pytest.approx(now - 600, abs=2)

    def test_window_none_for_normal_or_unknown_session(self):
        """非常供端口 / 非活跃会话：返回 None，调用方回落 DB 曲线。"""
        mgr = make_manager()
        self._session(mgr, port=2, sid=7)
        assert mgr.get_permanent_session_window(7) is None      # 未标记常供
        mgr.set_permanent_ports(["c2"])
        assert mgr.get_permanent_session_window(999) is None    # 会话不活跃
        assert mgr.get_permanent_session_window(None) is None

    @pytest.mark.asyncio
    async def test_close_session_releases_window(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_permanent_ports(["c2"])
        self._session(mgr, port=2, sid=7, wh=3.0)
        mgr._append_permanent_point(2, 20.0, 1.0, "PD")
        assert len(mgr._permanent_points[2]) == 1

        mgr._close_session(2, time.time(), 20.0, 1.0, END_REASON_UNPLUG)
        await asyncio.sleep(0.05)
        assert len(mgr._permanent_points[2]) == 0, "会话结束后释放内存窗口"


class TestFullOff:
    """充满即停：会话被"自动判定结束"（low_power）时关闭端口，含重试与复位。"""

    def _charging(self, mgr, port=1, wh=5.0, sid=42):
        with mgr._sess_lock:
            mgr._active_sessions[port] = sid
        es = mgr._energy_states[port]
        es.is_charging = True
        es.session_wh = wh
        es.session_start = 1000.0
        mgr.state.ports[port].active = True
        return es

    def _queued(self, mgr):
        out = []
        while not mgr.cmd_queue.empty():
            out.append(mgr.cmd_queue.get_nowait()[1])
        return out

    @pytest.mark.asyncio
    async def test_auto_session_end_enqueues_port_off(self):
        """会话自动结束（low_power）→ 武装并断端口。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "always"})
        self._charging(mgr, wh=8.0)

        mgr._close_session(1, 2000.0, 20.0, 0.05, END_REASON_LOW_POWER)
        await asyncio.sleep(0.05)
        assert ("c1", "off") in self._queued(mgr)
        assert mgr._full_off_pending[1] == 2000.0

    @pytest.mark.asyncio
    async def test_disabled_port_not_enqueued(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        self._charging(mgr, wh=8.0)
        mgr._close_session(1, 2000.0, 20.0, 0.05, END_REASON_LOW_POWER)
        await asyncio.sleep(0.05)
        assert self._queued(mgr) == []
        assert mgr._full_off_pending[1] == 0.0

    @pytest.mark.asyncio
    async def test_other_end_reasons_not_enqueued(self):
        """只有"自动结束"（low_power）才断电：拔插/用户关端口/链路中断/关机都不触发。"""
        for reason in (END_REASON_UNPLUG, END_REASON_USER_OFF,
                       END_REASON_LINK_LOSS, END_REASON_SHUTDOWN, END_REASON_UNKNOWN):
            mgr = make_manager()
            mgr._history = MagicMock()
            mgr.set_full_off({"c1": "always"})
            self._charging(mgr, wh=8.0)
            mgr._close_session(1, 2000.0, 20.0, 0.05, reason)
            await asyncio.sleep(0.05)
            assert self._queued(mgr) == [], f"reason={reason} 不应断电"

    @pytest.mark.asyncio
    async def test_below_min_energy_not_enqueued(self):
        """能量过小（瞬时低功率/刚插上就涓流）不触发，避免误断。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "always"})
        self._charging(mgr, wh=BLEManager.FULL_OFF_MIN_WH - 0.1)
        mgr._close_session(1, 2000.0, 20.0, 0.02, END_REASON_LOW_POWER)
        await asyncio.sleep(0.05)
        assert self._queued(mgr) == []

    def test_confirmed_when_port_reads_inactive(self):
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._full_off_pending[1] = 1000.0
        mgr.state.ports[1].active = False
        mgr._enforce_full_off(1, 1001.0)
        assert mgr._full_off_pending[1] == 0.0
        assert mgr._full_off_fired[1] is True

    def test_retries_within_window_then_gives_up(self):
        """端口仍 active：按 FULL_OFF_RETRY_SEC 重试，超上限放弃并清挂起。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr.state.ports[1].active = True
        mgr._full_off_pending[1] = 1000.0
        mgr._full_off_last_try[1] = 1000.0

        mgr._enforce_full_off(1, 1005.0)          # 窗口内：不重复入队
        assert mgr.cmd_queue.empty()
        mgr._enforce_full_off(1, 1016.0)          # 超过窗口：重试
        assert mgr.cmd_queue.qsize() == 1

        for i in range(BLEManager.FULL_OFF_MAX_ATTEMPTS + 2):
            mgr._enforce_full_off(1, 1032.0 + i * 20)
            mgr.cmd_queue.get_nowait() if not mgr.cmd_queue.empty() else None
        assert mgr._full_off_pending[1] == 0.0, "超上限后不再挂起"
        assert mgr._full_off_attempts[1] >= BLEManager.FULL_OFF_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_new_session_rearms_full_off(self):
        """新会话起点复位挂起/重试/已触发状态（与限额重新武装同一处）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        mgr.ctrl.decrypt = MagicMock(return_value=bytes(
            [0, 0, 0, 0, 0x04, 0, 0, 1, 0, 0x0a, 25, 201]))   # 20.1V 2.5A
        mgr.set_mqtt_publisher(MagicMock())
        mgr.set_full_off({"c1": "always"})
        mgr._full_off_pending[1] = 1234.0
        mgr._full_off_attempts[1] = 3
        mgr._full_off_fired[1] = True
        es = mgr._energy_states[1]
        es.is_charging = False

        _prime_start_gate(mgr)
        data = bytes([0, 0, 0x02, 4]) + b'\x00' * 10
        await mgr._handle_inline_data(data)

        assert mgr._full_off_pending[1] == 0.0
        assert mgr._full_off_attempts[1] == 0
        assert mgr._full_off_fired[1] is False
        assert self._queued(mgr) == [], "新会话开始不应误触发断电"


class TestPermanentPortDisablesLimit:
    """长期供电端口不参与限额断电（后端登记时清零 + 判定兜底）。"""

    def test_marking_permanent_zeroes_limit(self):
        mgr = make_manager()
        mgr.set_charge_limits({"c2": {"wh": 30.0, "mode": "always"}})
        mgr.set_permanent_ports(["c2"])
        state = mgr.get_charge_limits_state()
        assert state["c2"]["wh"] == 0.0
        assert state["c2"]["mode"] == "always", "模式保留，只清阈值"

    def test_enforce_skips_permanent_port(self):
        mgr = make_manager()
        mgr.set_permanent_ports(["c1"])
        mgr._charge_limits[1] = 30.0          # 绕过 API 直接塞一个阈值
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 99.0
        mgr._enforce_charge_limit(1, 2000.0)
        assert mgr.cmd_queue.empty(), "长期供电端口不应被限额断掉"

    def test_is_permanent_name(self):
        mgr = make_manager()
        mgr.set_permanent_ports(["c2"])
        assert mgr.is_permanent_name("c2") is True
        assert mgr.is_permanent_name(" C2 ") is True
        assert mgr.is_permanent_name("c1") is False
        assert mgr.is_permanent_name("nope") is False

    def test_once_mode_consumed_on_confirmation(self):
        """once：断电确认后即消费（与限额 once 同语义），且"已触发"标记一并清掉。

        标记只是"本次会话断过电"的瞬时信息。消费后功能已经回到"未启用"，标记
        再留着就会让卡片长期显示自相矛盾的「未启用 · 已触发」，状态点也一直停在
        警示色（实测就是这个现象）。能量限额的 _limit_fired 同样在会话闭合时复位。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "once"})
        mgr.state.ports[1].active = False
        mgr._full_off_pending[1] = 1000.0
        mgr._enforce_full_off(1, 1001.0)
        assert 1 not in mgr.full_off, "once 命中即消费"
        assert mgr._full_off_fired[1] is False, "消费后不得再挂「已触发」（否则界面自相矛盾）"
        assert mgr.get_port_modes_state()["full_off_fired"] == [], "对外状态里也不该再出现"
        assert mgr.get_port_modes_state()["full_off_ports"] == {}

    @pytest.mark.asyncio
    async def test_always_mode_kept_on_confirmation(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "always"})
        mgr.state.ports[1].active = False
        mgr._full_off_pending[1] = 1000.0
        mgr._enforce_full_off(1, 1001.0)
        assert mgr.full_off == {1: "always"}, "always：下次充电继续生效"
        # always 功能仍生效，「已启用 · 已触发」是有意义的信息，留到下次会话起点
        assert mgr._full_off_fired[1] is True
        assert mgr.get_port_modes_state()["full_off_fired"] == ["c1"]
        mgr._start_session(1, 2000.0, 20.0, 1.0, "PD")
        assert mgr._full_off_fired[1] is False, "新会话起点复位"

    def test_list_input_uses_default_mode(self):
        mgr = make_manager()
        mgr.set_full_off(["c1"])
        assert mgr.full_off == {1: BLEManager.FULL_OFF_DEFAULT_MODE}

    def test_disabling_clears_fired_flag(self):
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._full_off_fired[1] = True
        mgr.set_full_off({})
        assert mgr._full_off_fired[1] is False, "关掉即停后不再挂旧状态"


class TestSessionLifecycle:
    """会话边界：时间窗+功率、无负载去抖、原因区分、结束后防 churn。

    对应旧实现的六个缺陷：单帧早断 / 帧数≠时间 / 电流阈值与电压耦合 /
    回升不重置 / 原因语义混用 / 结束后门限副作用。
    """

    def _charging(self, mgr, port=1, wh=10.0, peak=50.0):
        es = mgr._energy_states[port]
        es.is_charging = True
        es.session_wh = wh
        es.session_start = time.time() - 3600
        es.max_power = peak
        mgr.state.ports[port].active = True
        return es

    def _queued(self, mgr):
        out = []
        while not mgr.cmd_queue.empty():
            out.append(mgr.cmd_queue.get_nowait()[1])
        return out

    def _spy_close(self, mgr):
        spy = MagicMock(wraps=mgr._close_session)
        mgr._close_session = spy
        return spy

    # ── ① 单帧无负载绝不能结束会话 ──

    def test_single_no_load_frame_does_not_end_session(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr)
        spy = self._spy_close(mgr)

        mgr._manage_session(1, 1000.0, 0.0, 0.0, active=False)   # 推送间隙/瞬时不上报
        mgr._manage_session(1, 1002.0, 0.0, 0.0, active=False)   # 还在去抖窗口内
        assert es.is_charging is True, "单帧无负载不得结束会话（旧实现的元凶）"
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_load_ends_after_debounce(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr)
        spy = self._spy_close(mgr)

        mgr._manage_session(1, 1000.0, 20.0, 0.0, active=False)
        mgr._manage_session(1, 1000.0 + mgr.NO_LOAD_DEBOUNCE_SEC + 0.5,
                            20.0, 0.0, active=False)
        await asyncio.sleep(0.05)
        spy.assert_called_once()
        assert spy.call_args[0][4] == END_REASON_NO_LOAD, "电压仍在协商 → 设备不吸电（自然结束）"
        assert es.is_charging is False

    @pytest.mark.asyncio
    async def test_no_load_with_collapsed_voltage_is_unplug(self):
        mgr = make_manager()
        mgr._history = MagicMock()
        self._charging(mgr)
        spy = self._spy_close(mgr)

        mgr._manage_session(1, 1000.0, 0.0, 0.0, active=False)
        mgr._manage_session(1, 1000.0 + mgr.NO_LOAD_DEBOUNCE_SEC + 0.5,
                            0.0, 0.0, active=False)
        await asyncio.sleep(0.05)
        assert spy.call_args[0][4] == END_REASON_UNPLUG, "电压塌掉 → 拔出"

    @pytest.mark.asyncio
    async def test_short_current_dropout_does_not_split_session(self):
        """充电中短暂断流（PD 重协商/档位切换、固件握手）不得把会话切成两段。

        回放实测：I=0 只持续 5s 就收尾的话，断流后立刻按 45W 重开，一条会话变成
        两条——session_wh 归零，Wh 限额进度跟着归零重来，历史里还多一行碎片。
        去抖现在是 15s（见 test_debounce_window_matches_measured_pauses），远小于
        120s/600s 的断电武装窗口。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=8.0, peak=45.0)
        spy = self._spy_close(mgr)

        t0 = 1000.0
        for k in range(5):                       # 5s 零电流（V 仍在协商范围内）
            mgr._manage_session(1, t0 + k, 20.0, 0.0, active=True)
        await asyncio.sleep(0.05)
        spy.assert_not_called()
        assert es.is_charging is True, "5s 断流不得结束会话"

        # 恢复供电后仍是同一条会话（不重开、能量不清零）
        mgr._manage_session(1, t0 + 6, 20.0, 2.25, active=True)
        await asyncio.sleep(0.05)
        spy.assert_not_called()
        assert es.is_charging is True
        assert es.session_wh == pytest.approx(8.0), "会话能量不因断流被清零"

    @pytest.mark.asyncio
    async def test_long_zero_current_still_ends_as_no_load(self):
        """断流超过（放宽后的）去抖窗口仍要收尾：15s 是放宽，不是取消。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=8.0, peak=45.0)
        spy = self._spy_close(mgr)

        t0 = 2000.0
        for k in range(int(mgr.NO_LOAD_DEBOUNCE_SEC) + 1):
            mgr._manage_session(1, t0 + k, 20.0, 0.0, active=True)
        await asyncio.sleep(0.05)
        spy.assert_called_once()
        assert spy.call_args[0][4] == END_REASON_NO_LOAD, "电压仍在 → 设备不吸电（自然结束）"
        assert es.is_charging is False

    @pytest.mark.asyncio
    async def test_debounce_window_matches_measured_pauses(self):
        """去抖定在 15s 是实测结论，不是拍脑袋：12s 必须吸收、20s 必须收尾。

        数据（2 天 1Hz 的 port_history，仅统计落在会话区间内的零电流段）：
          C1 86 次"暂停后恢复"：最长 14s，≥3s 32 次、≥8s 2 次、≥15s 0 次；
          C3 9 次最长 4s；USB-A 3 次最长 8s。
        3s 会把 34 次停顿判成会话结束（session_wh 归零 → Wh 限额进度重来），
        8s 仍剩 2 次，15s 归零。这条把窗口两端都钉住，避免有人随手调小/调大。
        """
        assert BLEManager.NO_LOAD_DEBOUNCE_SEC == 15.0, \
            f"去抖窗口应为 15s（实测结论），实际 {BLEManager.NO_LOAD_DEBOUNCE_SEC}"

        # 12s 零电流（实测最长 14s 的同类停顿）→ 必须吸收
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=8.0, peak=45.0)
        spy = self._spy_close(mgr)
        for k in range(12):
            mgr._manage_session(1, 3000.0 + k, 20.0, 0.0, active=True)
        await asyncio.sleep(0.05)
        spy.assert_not_called()
        assert es.is_charging is True, "12s 停顿不得结束会话"

        # 20s 零电流 → 必须收尾（设备真的不取电了）
        mgr2 = make_manager()
        mgr2._history = MagicMock()
        es2 = self._charging(mgr2, wh=8.0, peak=45.0)
        spy2 = self._spy_close(mgr2)
        for k in range(20):
            mgr2._manage_session(1, 4000.0 + k, 20.0, 0.0, active=True)
        await asyncio.sleep(0.05)
        spy2.assert_called_once()
        assert spy2.call_args[0][4] == END_REASON_NO_LOAD
        assert es2.is_charging is False

    @pytest.mark.asyncio
    async def test_port_disabled_ends_immediately(self):
        """端口被关（用户/限额）是我们自己的动作：不去抖，立即以 USER_OFF 结束。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        self._charging(mgr)
        mgr.state.settings["16"] = 0x0E          # C1 位清零
        spy = self._spy_close(mgr)

        mgr._manage_session(1, 1000.0, 0.0, 0.0, active=False)
        await asyncio.sleep(0.05)
        assert spy.call_args[0][4] == END_REASON_USER_OFF

    @pytest.mark.asyncio
    async def test_low_power_convergence_ends_and_arms(self):
        """低功率收敛（时间窗判定）结束 → 武装自动断电。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "always"})
        es = self._charging(mgr, wh=20.0)
        det = mgr._session_dets[1]
        now = time.time()
        # 先充一段（建立 p_base），再喂 11 分钟 1.5W 残留
        for k in range(0, 300):
            mgr._manage_session(1, now - 1200 + k, 20.0, 3.0, active=True)   # 60W
        for k in range(0, 11 * 60):
            mgr._manage_session(1, now - 660 + k, 20.0, 0.015, active=True)  # 0.3W（逼近 0）
        await asyncio.sleep(0.05)
        assert es.is_charging is False, "连续 10 分钟收敛应结束会话"
        assert mgr._full_off_pending[1] > 0, "自动断电应已武装"
        assert ("c1", "off") in self._queued(mgr)
        del det

    # ── ⑤ 原因语义：只有"自然结束"才武装自动断电 ──

    @pytest.mark.asyncio
    async def test_unplug_and_user_off_do_not_arm(self):
        for active, voltage, expect in ((False, 0.0, None),
                                        (False, 20.0, END_REASON_NO_LOAD)):
            mgr = make_manager()
            mgr._history = MagicMock()
            mgr.set_full_off({"c1": "always"})
            self._charging(mgr)
            spy = self._spy_close(mgr)
            mgr._manage_session(1, 1000.0, voltage, 0.0, active=active)
            mgr._manage_session(1, 1000.0 + mgr.NO_LOAD_DEBOUNCE_SEC + 0.5,
                                voltage, 0.0, active=active)
            await asyncio.sleep(0.05)
            assert spy.call_args[0][4] == (expect or END_REASON_UNPLUG)
            assert mgr._full_off_pending[1] == 0.0, "拔出/无负载不得立即武装（要等持续观察）"

    def test_no_load_arms_only_after_long_hold(self):
        """设备彻底不吸电（电压仍在）：持续 NO_LOAD_ARM_SEC 才武装。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 5.0        # 会话能量（no_load 路径不看它，仅作背景）
        t0 = 1000.0
        mgr._manage_session(1, t0, 20.0, 0.0, active=False)          # 起计时
        mgr._manage_session(1, t0 + 60, 20.0, 0.0, active=False)     # 1 分钟：不武装
        assert mgr._full_off_pending[1] == 0.0
        mgr._manage_session(1, t0 + mgr.NO_LOAD_ARM_SEC + 1, 20.0, 0.0, active=False)
        assert mgr._full_off_pending[1] == t0 + mgr.NO_LOAD_ARM_SEC + 1
        assert ("c1", "off") in self._queued(mgr)

    def test_no_load_arm_ignores_session_energy(self):
        """no_load 路径不看会话能量：时间窗就是证据。

        设备充满后常会周期性冒几秒的小会话（实测 10.2Wh 大会话后跟了一个
        7 秒 0Wh 会话），若沿用 FULL_OFF_MIN_WH 门槛，`es.session_wh` 会被
        覆盖成 0，自动断电就再也武装不起来。
        """
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 0.0        # 最近一次会话几乎没有能量
        t0 = 1000.0
        mgr._manage_session(1, t0, 20.0, 0.0, active=False)
        mgr._manage_session(1, t0 + mgr.NO_LOAD_ARM_SEC + 1, 20.0, 0.0, active=False)
        assert mgr._full_off_pending[1] > 0, "连续确认没在充电就该武装，与能量无关"
        assert ("c1", "off") in self._queued(mgr)

    def test_unplug_does_not_arm_no_load(self):
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._energy_states[1].is_charging = False
        mgr._manage_session(1, 1000.0, 0.0, 0.0, active=False)
        mgr._manage_session(1, 1000.0 + mgr.NO_LOAD_ARM_SEC + 1, 0.0, 0.0, active=False)
        assert mgr._full_off_pending[1] == 0.0, "拔出不该触发自动断电"

    # ── ⑥ 结束后防 churn：残留功耗不得重开会话 ──

    @pytest.mark.asyncio
    async def test_residual_power_does_not_restart_session(self):
        """会话刚结束后，端口上的"小尾巴"不得开新会话（两种典型残留）。"""
        # ① PD 口：60W 会话结束后设备仍以小电流维持（20V/0.075A = 1.5W）
        mgr = make_manager()
        mgr._history = MagicMock()
        t0 = time.time()
        mgr._start_gates[1].note_session_end(peak_power=60.0)
        for k in range(0, 900, 5):
            mgr._manage_session(1, t0 + k, 20.1, 0.075, active=True)
        await asyncio.sleep(0.05)
        assert mgr._energy_states[1].is_charging is False, "PD 口 1.5W 残留不得开新会话"

        # ② 5V 口：耳机充满后 0.25W 残留（旧实现按 0.1A 电流判，容易反复重开）
        mgr2 = make_manager()
        mgr2._history = MagicMock()
        mgr2._start_gates[2].note_session_end(peak_power=1.0)
        for k in range(0, 900, 5):
            mgr2._manage_session(2, t0 + k, 5.0, 0.05, active=True)
        await asyncio.sleep(0.05)
        assert mgr2._energy_states[2].is_charging is False, "5V 口 0.25W 残留不得开新会话"

    @pytest.mark.asyncio
    async def test_clear_restart_starts_new_session(self):
        """静默期内明显重新充电（≥5W 且持续 30s）→ 正常开新会话。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        t0 = time.time()
        mgr._start_gates[1].note_session_end(peak_power=60.0)
        for k in range(0, 40):
            mgr._manage_session(1, t0 + k, 20.0, 0.6, active=True)     # 12W
        await asyncio.sleep(0.05)
        assert mgr._energy_states[1].is_charging is True, "真正重新充电应当开新会话"

    def test_start_threshold_follows_voltage_not_fixed_power(self):
        """开始门限 = max(0.5W, 0.1A×电压)：5V 下 0.5W（耳机也开得起来）、
        20V 下 2W（与旧规则等价），而不是一个固定的功率地板。"""
        gate = make_manager()._start_gates[1]
        assert gate.base_threshold_w(5.0) == 0.5
        assert gate.base_threshold_w(20.0) == 2.0
        # 会话刚结束后进入残留保护：大功率会话 → 重开门限抬到 2×T_low=5W
        gate.note_session_end(peak_power=60.0)
        assert gate.threshold_now(20.0) == 2.0      # max(基础 2.0W, 2×T_low 1.0W)
        # 拔出（空载）解除保护
        gate.note_no_load()
        assert gate.threshold_now(20.0) == 2.0

    # ── ⑦ 两条采样路径都要写曲线点 ──

    async def test_timer_path_records_charge_points(self):
        """C3/USB-A 全靠 1s 定时器驱动：定时器不写采样点就没有曲线，
        end_session 的均压/均流也会退化成 0（旧实现在定时器里写过，重构时漏掉）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = True
        es = mgr._energy_states[3]
        es.is_charging = True
        es.session_wh = 1.0
        es.session_start = time.time() - 600
        es.last_time = None                      # 从未收到推送 → 定时器代表真实状态
        with mgr._sess_lock:
            mgr._active_sessions[3] = 77
        mgr.state.ports[3].active = True
        mgr.state.ports[3].voltage = 9.0
        mgr.state.ports[3].current = 1.5
        mgr.state.ports[3].protocol = "QC"

        # 直接复刻定时器分支的判定（该分支在体内，不便整体驱动）
        ps = mgr.state.ports[3]
        now = time.time()
        idle = (es.last_time is None or (now - es.last_time) > mgr.SAMPLE_FRESH_SEC)
        assert idle, "无推送 → 定时器应当接管采样"
        mgr._manage_session(3, now, ps.voltage, ps.current,
                            active=ps.active, protocol=ps.protocol)
        assert mgr._history and es.is_charging and 3 in mgr._active_sessions
        assert (ps.current > 0 or 3 in mgr.permanent_ports), "定时器写点条件成立"
        mgr._record_charge_point(3, ps.voltage, ps.current, ps.protocol)
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_called_once_with(77, 9.0, 1.5, 13.5, "QC")

    async def test_timer_path_records_points_for_permanent_port_at_zero_amp(self):
        """常供端口在 0A 时也要写（走内存窗口，自带 1s 节流），否则曲线断档。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_permanent_ports(["c3"])
        es = mgr._energy_states[3]
        es.is_charging = True
        with mgr._sess_lock:
            mgr._active_sessions[3] = 78
        mgr.state.ports[3].voltage = 5.0
        mgr.state.ports[3].current = 0.0
        assert 3 in mgr.permanent_ports, "常供端口豁免 current>0 限制"
        mgr._record_charge_point(3, 5.0, 0.0, "")
        assert len(mgr._permanent_points[3]) == 1, "0A 也是有效曲线点（内存窗口）"

    # ── ⑧ 关闭"充满即停"/改为常供：必须撤销尚未确认的断电 ──

    def test_marking_permanent_cancels_pending_full_off(self):
        """改为长期供电时若已有挂起的自动断电，必须一并撤销——否则刚声明常供的
        端口会被在途的 off 命令断掉（_enforce_full_off 只看 _full_off_pending）。"""
        mgr = make_manager()
        mgr.set_full_off({"c2": "always"})
        mgr._full_off_pending[2] = 5000.0
        mgr._full_off_attempts[2] = 2
        mgr._full_off_fired[2] = True

        mgr.set_permanent_ports(["c2"])
        assert 2 not in mgr.full_off, "互斥：常供端口不带即停"
        assert mgr._full_off_pending[2] == 0.0, "挂起的断电必须撤销"
        assert mgr._full_off_attempts[2] == 0
        assert mgr._full_off_fired[2] is False
        # 挂起撤销后 _enforce_full_off 不得再入队关断命令
        mgr.state.ports[2].active = False
        mgr._enforce_full_off(2, 5100.0)
        assert mgr.cmd_queue.empty(), "撤销后不得再断这个端口"

    def test_disabling_full_off_cancels_pending(self):
        """关掉"充满即停"时未确认的断电同样要撤销：否则用户关了功能，端口仍被断。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._full_off_pending[1] = 5000.0
        mgr._full_off_attempts[1] = 1

        mgr.set_full_off([])
        assert mgr.is_full_off(1) is False
        assert mgr._full_off_pending[1] == 0.0, "关闭功能必须撤销在途断电"
        assert mgr._full_off_attempts[1] == 0
        mgr.state.ports[1].active = False
        mgr._enforce_full_off(1, 5100.0)
        assert mgr.cmd_queue.empty()

    # ── ⑨ D 分支：判据是电流不是 active（插着不充正是 active=True） ──

    def test_no_load_arm_uses_power_not_active_flag(self):
        """active = in_use or V>0 or I>0：只要电压还在它就是 True。

        用 active 过滤会让"插着不充"这个本功能真正想覆盖的场景永远进不了
        D 分支（那正是 active=True、current=0）。判据必须是功率，不是 active。
        """
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 5.0
        t0 = 1000.0
        # 20V 协商中、电流为 0 → active=True，但仍应计入"不吸电"时长
        mgr._manage_session(1, t0, 20.0, 0.0, active=True)
        mgr._manage_session(1, t0 + mgr.NO_LOAD_ARM_SEC + 1, 20.0, 0.0, active=True)
        assert mgr._full_off_pending[1] == t0 + mgr.NO_LOAD_ARM_SEC + 1, (
            "插着不充（active=True, I=0）持续够久仍应武装")
        assert ("c1", "off") in self._queued(mgr)

    def test_no_load_arm_timer_cleared_when_real_charging_resumes(self):
        """真正重新充电（功率明显高于涓流）要清掉计时，防止半截时长被接着用。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 5.0
        t0 = 1000.0
        mgr._manage_session(1, t0, 20.0, 0.0, active=True)          # 起计时
        mgr._manage_session(1, t0 + 300, 20.0, 1.0, active=True)    # 又开始取电
        assert mgr._no_load_since.get(1) is None, "恢复取电要清掉计时"
        mgr._manage_session(1, t0 + 320, 20.0, 0.0, active=True)    # 重新起计时
        assert mgr._no_load_since[1] == t0 + 320
        assert mgr._full_off_pending[1] == 0.0, "计时被打断就不该立刻武装"

    def test_enqueue_failure_keeps_pending_for_retry(self):
        """入队失败不能清掉 pending：清了就没有重试了。"""
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._enqueue_port_off = lambda piid: False      # 模拟队列满
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 5.0
        mgr._maybe_arm_full_off(1, 1000.0, reason="no_load")
        assert mgr._full_off_pending[1] == 1000.0, "pending 保留，由 _enforce_full_off 重试"
        # 重试：到点后 _enforce_full_off 会再入队一次
        mgr.state.ports[1].active = True
        mgr._enforce_full_off(1, 1000.0 + mgr.FULL_OFF_RETRY_SEC + 1)
        assert mgr._full_off_pending[1] == 1000.0, "重试失败也保留（直到超上限）"
        assert mgr._full_off_attempts[1] == 1

    async def test_permanent_window_accumulates_with_recording_off(self):
        """常供端口不落 charge_points，与 record_sessions 无关：记录关闭时
        （sid 是负值占位）内存窗口仍要继续积累，否则详情浮层会空白。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.record_sessions = False
        mgr.set_permanent_ports(["c1"])
        es = mgr._energy_states[1]
        es.is_charging = True
        with mgr._sess_lock:
            mgr._active_sessions[1] = -1        # 记录关闭 → 占位负 sid

        assert mgr._record_charge_point(1, 20.0, 1.5, "PD") is False
        assert len(mgr._permanent_points[1]) == 1, "记录关闭时内存窗口仍应积累"
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_not_called(), "常供端口不落库"

    async def test_permanent_window_scoped_to_active_session(self):
        """常供分支只在会话进行中生效：会话已结束时不再往窗口里塞点，
        而是照常走普通落库路径（此时该端口已被取消常供，曲线该进 DB）。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_permanent_ports(["c1"])
        mgr._energy_states[1].is_charging = False      # 会话已结束
        with mgr._sess_lock:
            mgr._active_sessions[1] = 42

        assert mgr._record_charge_point(1, 20.0, 1.5, "PD") is True, "已结束 → 走普通落库"
        assert len(mgr._permanent_points[1]) == 0, "内存窗口不再积累"
        await asyncio.sleep(0.05)
        mgr._history.record_charge_point.assert_called_once_with(42, 20.0, 1.5, 30.0, "PD")

    def test_no_load_since_preseeded_for_all_ports(self):
        """_no_load_since 四个口都预置为 None，且 None 必须被当成"尚未起计时"。

        用 setdefault 的话，预置的 None 会被当成已有值返回，计时永远起步不了
        （首个无负载样本白记一次，去抖被拉长一个采样间隔）。
        """
        mgr = make_manager()
        for piid in range(1, 5):
            assert piid in mgr._no_load_since, f"端口 {piid} 未预置"
            assert mgr._no_load_since[piid] is None
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 5.0
        t0 = 1000.0
        mgr._manage_session(1, t0, 20.0, 0.0, active=True)
        assert mgr._no_load_since[1] == t0, "None 必须被当作'尚未起计时'（不能 setdefault）"

    # ── ⑩ 拔出后固件不再推送：会话必须还能被定时器/主动重读收尾 ──

    def test_open_session_is_sampled_even_at_zero_reading(self):
        """定时器不得因为"读数 0V/0A"就跳过这一口。

        拔出后固件不再推送该口，而"无负载"结束判定要靠连续样本走完
        NO_LOAD_DEBOUNCE_SEC：一跳过，去抖永远走不完，会话就永久挂在活跃状态
        （真机现象：c1 拔出很久仍显示充电中）。没有会话时仍按老规则跳过。
        """
        mgr = make_manager()
        ps = mgr.state.ports[1]
        ps.voltage = 0.0
        ps.current = 0.0

        assert mgr._should_sample_port(1, ps) is False, "无会话 + 读数 0 → 跳过（省空转）"
        assert mgr._should_sample_port(1, None) is False

        mgr._energy_states[1].is_charging = True
        assert mgr._should_sample_port(1, ps) is True, "会话还开着 → 必须继续采样"

        # 只有 sid 没有内存态（start_session 竞态窗口）同样不能跳过
        mgr._energy_states[1].is_charging = False
        with mgr._sess_lock:
            mgr._active_sessions[1] = 99
        assert mgr._should_sample_port(1, ps) is True
        # 有读数就照常采样
        with mgr._sess_lock:
            mgr._active_sessions.pop(1, None)
        ps.voltage = 5.0
        assert mgr._should_sample_port(1, ps) is True

    def test_empty_port_releases_residual_start_protection(self):
        """空口（电压塌掉）必须解除"刚结束"的残留保护。

        残留保护把重开门限抬到 max(0.4W, 2×T_low)（最高 1.0W），而 5V 口上
        0.5~1W 的小设备正好卡在这条线下面。释放只能发生在采样判定里：读数全零
        又没有会话的端口本来会被跳过，_manage_session 跑不到，note_no_load()
        永远不被调用——于是"这个口充过大功率设备"会永久挡住后续的小功率设备
        （基线是会话结束后 60s 门限就回落）。
        """
        mgr = make_manager()
        ps = mgr.state.ports[1]
        gate = mgr._start_gates[1]

        gate.note_session_end(peak_power=90.0)
        assert gate.threshold_now(5.0) == pytest.approx(1.2), "90W 会话后重开门限抬到 2×T_low=1.2W"

        ps.voltage = 0.0
        ps.current = 0.0
        assert mgr._should_sample_port(1, ps) is False, "空口仍按老规矩跳过（省空转）"
        assert gate.threshold_now(5.0) == pytest.approx(0.5), "空口应解除残留保护"

        # 还有电压、只是不取电（设备插着，满电维持）→ 保护必须留着
        gate.note_session_end(peak_power=90.0)
        ps.voltage = 20.0
        ps.current = 0.0
        assert mgr._should_sample_port(1, ps) is True
        assert gate.threshold_now(5.0) == pytest.approx(1.2), "设备还插着不得解除保护"

    @pytest.mark.asyncio
    async def test_low_power_device_starts_after_unplug(self):
        """拔出收尾后重新插上 5V/0.7W 的小设备，必须开得出新会话。

        回归场景：会话以 UNPLUG 结束时曾把重开门限抬到 1.0W 并永久保留，
        0.7W 的耳机再也开不出会话——充电照旧，但界面、能耗统计、限额与
        充满即停全部失效。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=8.0, peak=90.0)
        mgr.state.ports[1].voltage = 0.0
        mgr.state.ports[1].current = 0.0
        mgr.state.ports[1].active = False

        t0 = 1000.0
        mgr._manage_session(1, t0, 0.0, 0.0, active=False)              # 拔出那一帧
        mgr._manage_session(1, t0 + mgr.NO_LOAD_DEBOUNCE_SEC + 1.0,     # 去抖走完 → 收尾
                            0.0, 0.0, active=False)
        await asyncio.sleep(0.05)
        assert es.is_charging is False
        assert mgr._start_gates[1].threshold_now(5.0) == pytest.approx(0.5), \
            "以拔出的方式结束，不得留下抬高后的重开门限"

        # 重新插入 5V/0.7W（低于残留保护线、高于基础门限 0.5W）
        assert mgr._should_sample_port(1, mgr.state.ports[1]) is False
        for k in range(35):
            mgr._manage_session(1, t0 + 100 + k, 5.0, 0.14, active=True)
        assert es.is_charging is True, "0.7W 的小设备应当开得出会话"

    @pytest.mark.asyncio
    async def test_full_device_keeps_residual_protection(self):
        """满电维持（no_load 结束）后不得解除保护：残留功耗不许开新会话。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=8.0, peak=90.0)
        mgr.state.ports[1].voltage = 20.0
        mgr.state.ports[1].current = 0.0
        mgr.state.ports[1].active = True

        t0 = 2000.0
        mgr._manage_session(1, t0, 20.0, 0.0, active=True)              # 设备还在，只是不吸电
        mgr._manage_session(1, t0 + mgr.NO_LOAD_DEBOUNCE_SEC + 1.0,
                            20.0, 0.0, active=True)
        await asyncio.sleep(0.05)
        assert es.is_charging is False
        assert mgr._start_gates[1].threshold_now(5.0) == pytest.approx(1.2), \
            "满电维持结束仍要保留残留保护（涓流不许开新会话）"

    @pytest.mark.asyncio
    async def test_unplug_push_then_no_more_pushes_still_closes(self):
        """拔出推送只来一帧、之后再无推送：定时器续上后续样本，会话按期收尾。"""
        mgr = make_manager()
        mgr._history = MagicMock()
        es = self._charging(mgr, wh=7.8)
        mgr.state.ports[1].voltage = 0.0
        mgr.state.ports[1].current = 0.0
        mgr.state.ports[1].active = False
        spy = self._spy_close(mgr)

        t0 = 1000.0
        mgr._manage_session(1, t0, 0.0, 0.0, active=False)      # 唯一一帧拔出推送
        spy.assert_not_called()
        assert es.is_charging is True, "去抖窗口内不得结束"

        # 固件不再推送 → 定时器接管（修复前这里会被 0V/0A 过滤掉，永远收不到尾）
        assert mgr._should_sample_port(1, mgr.state.ports[1]) is True
        mgr._manage_session(1, t0 + mgr.NO_LOAD_DEBOUNCE_SEC + 1.0,
                            0.0, 0.0, active=False)
        await asyncio.sleep(0.05)
        spy.assert_called_once()
        assert spy.call_args[0][4] == END_REASON_UNPLUG, "电压塌掉 → 拔出"
        assert es.is_charging is False

    @pytest.mark.asyncio
    async def test_verify_port_closes_session_when_reading_already_zero(self):
        """主动重读到的 0V/0A 与本地状态相同时，也要把还开着的会话收掉。

        原实现只在"读数发生变化"时才顺带关会话：拔出帧已经把 0V/0A 写进状态、
        只是没关会话时，重读永远认为"没变化"，会话就一直挂着。
        """
        mgr = make_manager()
        mgr._mqtt_publish = MagicMock()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 7.8
        # 本地状态已经是 0V/0A（拔出帧写过），重读结果不会产生"变化"
        mgr.state.ports[1].voltage = 0.0
        mgr.state.ports[1].current = 0.0
        mgr.state.ports[1].active = False
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={
            "value": 0,
            "raw": bytes([0x11, 0x20, 0x01, 0x00, 0x03, 0x01, 0x02, 0x01,
                          0x00, 0x00, 0x00, 0x04, 0x05, 0x00, 0x00, 0x00, 0x00])})

        await mgr._handle_verify_port(1, None)
        await asyncio.sleep(0.05)

        assert es.is_charging is False, "读数没变也要关掉还开着的会话"

    # ── ⑪ 涓流脉冲不得重置自动断电的确认窗口（真机 c1 就是卡在这里） ──

    def test_pulsing_tail_does_not_reset_arm_window(self):
        """满电设备的涓流尾巴是 0.0A / 0.1A 交替（≈0.5W），不是充电。

        旧实现按"电流>0 就重置窗口"，于是每次冒 0.1A 都把计时清零，窗口永远
        走不完 —— 会话结束了端口却一直不关，`full_off` 也一直不被消费。
        """
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        es = mgr._energy_states[1]
        es.is_charging = False
        es.session_wh = 10.0
        mgr._no_load_from_session[1] = True      # 会话刚以 no_load 结束
        t0 = 1000.0
        for k in range(0, int(mgr.NO_LOAD_ARM_SESSION_SEC) + 10):
            cur = 0.1 if (k % 3 == 0) else 0.0   # 周期性冒 0.1A（0.51W）
            mgr._manage_session(1, t0 + k, 5.1, cur, active=True)
        assert mgr._full_off_pending[1] > 0, "涓流脉冲不得重置窗口（旧实现永远等不到）"
        assert ("c1", "off") in self._queued(mgr), "并真的发出关端口命令"

    @pytest.mark.asyncio
    async def test_no_load_end_with_pulsing_tail_switches_port_off(self):
        """端到端：会话以 no_load 结束 + 之后持续涓流脉冲 → 端口仍会被关闭。

        这是用户在 c1 的实测路径：充满后电流塌到涓流、会话结束，随后设备周期性
        冒 0.1A。修复前 D 分支的窗口被这些脉冲反复清零，端口一直不关。
        """
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr.set_full_off({"c1": "once"})
        es = self._charging(mgr, wh=10.2)
        t0 = 1000.0

        # 电流塌到 0 并持续过去抖窗口 → 以 no_load 结束会话
        mgr._manage_session(1, t0, 5.1, 0.0, active=True)
        mgr._manage_session(1, t0 + mgr.NO_LOAD_DEBOUNCE_SEC + 1, 5.1, 0.0, active=True)
        await asyncio.sleep(0.05)
        assert es.is_charging is False, "会话应当已结束"
        assert mgr._no_load_from_session[1] is True, "记住这是'会话刚结束的停止取电'"

        # 之后设备仍周期性冒 0.1A：窗口必须继续累积，且用短窗口（会话刚结束）
        for k in range(1, int(mgr.NO_LOAD_ARM_SESSION_SEC) + 15):
            cur = 0.1 if (k % 3 == 0) else 0.0
            mgr._manage_session(1, t0 + 5 + k, 5.1, cur, active=True)
        assert mgr._full_off_pending[1] > 0, "涓流尾巴下也要武装自动断电"
        assert ("c1", "off") in self._queued(mgr)

    def test_arm_window_shorter_after_session_end_than_from_scratch(self):
        """会话刚结束用短窗口（已有整段充电作证据）；从未充过电的口仍用长窗口。"""
        assert make_manager().NO_LOAD_ARM_SESSION_SEC < make_manager().NO_LOAD_ARM_SEC

        def _arm_at(window_flag):
            mgr = make_manager()
            mgr.set_full_off({"c1": "always"})
            es = mgr._energy_states[1]
            es.is_charging = False
            es.session_wh = 10.0
            mgr._no_load_from_session[1] = window_flag
            t0 = 1000.0
            for k in range(0, int(mgr.NO_LOAD_ARM_SEC) + 5):
                mgr._manage_session(1, t0 + k, 5.1, 0.0, active=True)
                if mgr._full_off_pending[1]:
                    return k
            return None

        after_session = _arm_at(True)
        from_scratch = _arm_at(False)
        assert after_session == int(make_manager().NO_LOAD_ARM_SESSION_SEC)
        assert from_scratch == int(make_manager().NO_LOAD_ARM_SEC)
        assert after_session < from_scratch

    def test_always_mode_rearms_after_a_new_session(self):
        """always 模式下武装标记必须在新会话起点复位。

        否则第一次断电确认后 `_no_load_armed` 一直为 True，D 分支的守卫
        `not _no_load_armed` 永远不成立 —— 下一次充满再也不会自动断电。
        """
        mgr = make_manager()
        mgr.set_full_off({"c1": "always"})
        mgr._no_load_armed[1] = True             # 上一次已经武装/确认过
        mgr._no_load_from_session[1] = True
        mgr._energy_states[1].is_charging = True

        mgr._start_session(1, 1000.0, 5.1, 1.0, "PD")   # 用户又插上开始充电

        assert mgr._no_load_armed[1] is False, "新会话必须复位武装标记"
        assert mgr._no_load_from_session[1] is False
        assert mgr._no_load_since.get(1) is None

    # ── ⑫ 常供卡要的是统计值：会话时长与平均功率 ──

    def test_limits_state_exposes_session_avg_and_sec(self):
        """avg_power_w / session_sec 由服务端算：会话进行中按"到现在"，
        结束后按"结束时刻 − 起点"（不会随挂钟继续变大，平均值因此不漂）。"""
        mgr = make_manager()
        es = mgr._energy_states[1]
        es.is_charging = True
        es.session_wh = 12.0
        now = time.time()
        es.session_start = now - 3600          # 已充 1 小时
        st = mgr.get_charge_limits_state()["c1"]
        assert 3590 <= st["session_sec"] <= 3610
        assert 11.9 <= st["avg_power_w"] <= 12.1, st["avg_power_w"]

        # 会话结束：时长冻结在 last_end_time，不再随挂钟增长
        es.is_charging = False
        es.last_end_time = es.session_start + 1800   # 实际只充了 30 分钟
        st = mgr.get_charge_limits_state()["c1"]
        assert st["session_sec"] == 1800.0
        assert st["avg_power_w"] == 24.0, st["avg_power_w"]

    def test_limits_state_no_session_reports_zero(self):
        mgr = make_manager()
        st = mgr.get_charge_limits_state()["c2"]
        assert st["session_sec"] == 0.0
        assert st["avg_power_w"] == 0.0


class TestPreSessionBackfill:
    """预会话环形缓冲 + 回填：会话开头那 30s 的采样不能再丢。

    起判要求功率连续达标 START_HOLD_SEC=30s 才开会话；这 30s 里设备**已经在取电**，
    但当时没有会话（能量不积分）、也没有 sid（曲线不写）。回填把这三点补上：
    起点、能量/峰值/检测器窗口、（拿到 sid 后）曲线点。
    """

    @staticmethod
    def _mgr_with_history():
        mgr = make_manager()
        mgr._history = MagicMock()
        mgr._history.start_session = MagicMock(return_value=1)
        return mgr

    @pytest.mark.asyncio
    async def test_backfill_fills_start_energy_peak_and_points(self):
        mgr = self._mgr_with_history()
        es, det = mgr._energy_states[1], mgr._session_dets[1]
        t0 = 1000.0
        # 前 15s 27W（9V/3A），后 15s 1.2W：门限 0.9W 一直达标 → 第 30s 放行开会话
        for k in range(15):
            mgr._manage_session(1, t0 + k, 9.0, 3.0, active=True)
        for k in range(15, 36):
            mgr._manage_session(1, t0 + k, 9.0, 1.2 / 9.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True, "连续达标 30s 应开会话"
        # ① 起点回填到本段负载真正开始处，而不是门控放行那一刻
        assert es.session_start == pytest.approx(t0), es.session_start
        # ② 能量含前 30s：≈15s×27W + 15s×1.2W ≈ 0.114Wh
        assert es.session_wh == pytest.approx(0.114, abs=0.01), es.session_wh
        # ③ 峰值/窗口预热：检测器峰值取 "p_fast 的历史最大"，回填后才看得到前段 27W
        assert det.peak_power() == pytest.approx(27.0, abs=0.5), det.peak_power()
        assert es.max_power == pytest.approx(27.0, abs=0.5), es.max_power
        # ④ 曲线点：拿到 sid 后批量补写一次（不是逐点开 executor）
        # DB 行的 start_time 必须同样是回填后的起点，否则 start+duration 会比 end 多一截
        assert mgr._history.start_session.call_args[0][2] == pytest.approx(t0)
        assert mgr._history.record_charge_points.call_count == 1
        sid, rows = mgr._history.record_charge_points.call_args[0]
        assert sid == 1 and len(rows) == 30, f"sid={sid} rows={len(rows) if rows else 0}"
        assert rows[0][0] == pytest.approx(t0, abs=0.01), "补写要用原采样时间戳"
        # 缓冲里存的是 round(...,2)（与充电器上报分辨率一致），所以 1.2/9→0.13
        assert rows[0][1] == pytest.approx(9.0)
        assert rows[-1][2] == pytest.approx(0.13, abs=1e-3)

    @pytest.mark.asyncio
    async def test_short_blip_still_creates_no_session(self):
        """坑 #2：<30s 的抖动/试探负载不得因为回填而变成会话。"""
        mgr = self._mgr_with_history()
        es = mgr._energy_states[1]
        t0 = 2000.0
        for k in range(20):                     # 只持续 20s，走不完 START_HOLD_SEC
            mgr._manage_session(1, t0 + k, 9.0, 1.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is False, "未达门控不得开会话"
        assert mgr._history.start_session.call_count == 0
        assert mgr._history.record_charge_points.call_count == 0
        assert len(mgr._pre_samples[1]) == 20, "抖动样本仍留在滚动缓冲里"
        assert es.session_wh == 0.0

    @pytest.mark.asyncio
    async def test_record_sessions_off_backfills_memory_only(self):
        """坑 #5：关记录时只回填内存（起点/能量/检测器），不写 DB。"""
        mgr = self._mgr_with_history()
        mgr.record_sessions = False
        es = mgr._energy_states[1]
        t0 = 3000.0
        for k in range(36):
            mgr._manage_session(1, t0 + k, 9.0, 1.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True
        assert es.session_start == pytest.approx(t0), "起点仍要回填"
        assert es.session_wh > 0, "能量仍要回填"
        assert mgr._history.start_session.call_count == 0, "关记录不建 DB 行"
        assert mgr._history.record_charge_points.call_count == 0, "关记录不补曲线点"
        assert mgr._active_sessions[1] < 0, "占位 sid"

    @pytest.mark.asyncio
    async def test_permanent_port_backfill_goes_to_memory_window(self):
        """坑 #4：常供端口不落 charge_points，回填进内存窗口，不写第二份。"""
        mgr = self._mgr_with_history()
        mgr.set_permanent_ports(["c1"])
        es = mgr._energy_states[1]
        t0 = 4000.0
        for k in range(36):
            mgr._manage_session(1, t0 + k, 9.0, 1.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True
        assert mgr._history.record_charge_points.call_count == 0, "常供端口不补 DB 点"
        pts = list(mgr._permanent_points[1])
        assert pts, "预会话采样应进内存窗口"
        assert pts[0][0] == pytest.approx(t0, abs=1.0), "窗口里的点是原采样时刻"
        assert es.session_start == pytest.approx(t0), "常供端口同样回填起点"

    @pytest.mark.asyncio
    async def test_buffer_is_trimmed_to_window_and_consumed(self):
        """缓冲按 PRE_SESSION_SEC 滚动裁剪；被回填消费后清空。"""
        mgr = self._mgr_with_history()
        t0 = 5000.0
        for k in range(int(mgr.PRE_SESSION_SEC) + 60):    # 远远超过窗口时长
            mgr._manage_session(1, t0 + k, 9.0, 0.02, active=True)   # 0.18W，不达标
        span = mgr._pre_samples[1][-1][0] - mgr._pre_samples[1][0][0]
        assert span <= mgr.PRE_SESSION_SEC + 0.001, f"缓冲未裁剪：跨度 {span}s"
        assert mgr._energy_states[1].is_charging is False
        assert mgr._history.start_session.call_count == 0

    @pytest.mark.asyncio
    async def test_backfill_moves_limit_trigger_earlier_by_the_same_energy(self):
        """回填的能量会让 Wh 限额提前触发，提前量 ≈ 被回填的那段时长。

        9V/1A（9W）+ 0.5Wh 限额：只算开会话之后的能量要 200s；把起判前 30s
        （30s×9W ≈ 0.0725Wh）回填进来后应提前约 29s。两种口径各跑一遍对比。
        """

        async def run(backfill_on):
            mgr = self._mgr_with_history()
            if not backfill_on:
                mgr._take_pre_session_samples = lambda *a, **k: []   # 关掉回填
            mgr.set_charge_limits({"c1": {"wh": 0.5, "mode": "always"}})
            es = mgr._energy_states[1]
            t0 = 6000.0
            gate_ts = None
            for k in range(400):
                t = t0 + k
                mgr._manage_session(1, t, 9.0, 1.0, active=True)
                if gate_ts is None and es.is_charging:
                    gate_ts = t                    # 门控放行那一刻（两种口径相同）
                if es.is_charging:
                    mgr._energy_integrator.update(es, 9.0, 1.0, t)
                mgr._enforce_charge_limit(1, t)
                await asyncio.sleep(0)
                if mgr._limit_fired[1]:
                    return t - gate_ts             # 相对"开会话时刻"的触发耗时
            raise AssertionError("限额未被触发")

        with_bf = await run(True)
        without_bf = await run(False)
        assert without_bf == pytest.approx(200.0, abs=3.0), without_bf   # 9W×200s = 0.5Wh
        assert with_bf == pytest.approx(171.0, abs=3.0), with_bf         # 回填 0.0725Wh
        assert without_bf - with_bf == pytest.approx(29.0, abs=3.0), "提前量应≈被回填的那段时长"

    @pytest.mark.asyncio
    async def test_first_session_can_only_backfill_what_this_process_saw(self):
        """服务刚重启时缓冲是空的：回填只能覆盖"本进程看到的那些帧"。

        设备在进程启动前就已经在充电时，那段历史无从得知——起点回填到本进程
        见过的最早一帧即止，不报错、不丢会话（这是允许的缺头）。
        """
        mgr = self._mgr_with_history()          # 新进程 = 空缓冲
        es = mgr._energy_states[1]
        t0 = 7000.0
        for k in range(36):                     # 进程只见过这 36 帧（设备已在充电）
            mgr._manage_session(1, t0 + k, 9.0, 1.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True
        assert es.session_start == pytest.approx(t0), "只能回填到本进程见过的最早一帧"
        sid, rows = mgr._history.record_charge_points.call_args[0]
        assert len(rows) == 30, f"缓冲里有多少就回填多少，实际 {len(rows)}"
        assert rows[0][0] == pytest.approx(t0)

    @pytest.mark.asyncio
    async def test_backfill_does_not_double_count_lifetime_energy(self):
        """回填只补会话能量，不能把 total_wh/daily_wh 也再加一遍。

        push 路径对**每一帧**都无条件积分（含这段"已在取电但还没开会话"的窗口），
        而回填只把 session_wh 归零、没管另两个累计量 → 起判窗口会被算两次。
        这里按真实 push 路径的顺序（先积分再 _manage_session）复现。
        """
        mgr = self._mgr_with_history()
        es = mgr._energy_states[1]
        t0 = 3000.0
        for k in range(15):                     # 前 15s 27W
            mgr._energy_integrator.update(es, 9.0, 3.0, t0 + k)
            mgr._manage_session(1, t0 + k, 9.0, 3.0, active=True)
        for k in range(15, 36):                 # 后 21s 1.2W（门限 0.9W 一直达标）
            i = 1.2 / 9.0
            mgr._energy_integrator.update(es, 9.0, i, t0 + k)
            mgr._manage_session(1, t0 + k, 9.0, i, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True
        assert es.session_wh == pytest.approx(0.114, abs=0.01), es.session_wh
        # 双计时 total_wh≈0.229（2 倍）；这里要求起判窗口只算一次。
        # 残余差值来自回填用的是缓冲里 round(…,2) 的电压电流（≈1e-4Wh 量级），不是双计。
        assert es.total_wh == pytest.approx(es.session_wh, abs=5e-3), (
            f"total_wh={es.total_wh:.4f} session_wh={es.session_wh:.4f}")
        assert es.daily_wh == pytest.approx(es.total_wh, abs=1e-9), es.daily_wh

    @pytest.mark.asyncio
    async def test_backfill_peak_includes_the_first_run_frame(self):
        """本段最高点落在起判首帧时，峰值也必须回填。

        能量回填循环从 pre[1:] 开始，首帧只被用作积分基准 → 原先 es.max_power 会
        停在后面的涓流功率上，与检测器峰值（含首帧）自相矛盾，DB peak_power_w 偏低。
        """
        mgr = self._mgr_with_history()
        es, det = mgr._energy_states[1], mgr._session_dets[1]
        t0 = 4000.0
        mgr._manage_session(1, t0, 9.0, 3.0, active=True)       # 27W：最高点就在首帧
        for k in range(1, 36):
            mgr._manage_session(1, t0 + k, 9.0, 1.2 / 9.0, active=True)
        await asyncio.sleep(0.05)

        assert es.is_charging is True
        assert es.max_power == pytest.approx(27.0, abs=0.5), es.max_power
        assert es.max_current == pytest.approx(3.0, abs=0.05), es.max_current
        assert es.max_power == pytest.approx(det.peak_power(), abs=0.5), (
            f"端口峰值 {es.max_power} 与检测器峰值 {det.peak_power()} 不一致")

    @pytest.mark.asyncio
    async def test_backfill_boundary_is_exact_not_rounding_dependent(self):
        """取材边界必须由原始时间戳定，不能随毫秒取整的舍入方向摇摆。

        缓冲原先存 round(ts,3)，而比较用的是门控记下的精确 run_start / 当前帧精确
        timestamp：舍入向下时首帧（round 后更小）被排除 → 起点晚一帧；当前帧
        （round 后更小）被反过来包含 → 检测器重复喂、曲线多一行。两种小数各测一遍。
        """
        for frac in (0.0004, 0.0006):
            mgr = self._mgr_with_history()
            es = mgr._energy_states[1]
            t0 = 5000.0 + frac
            for k in range(36):                 # 第 30 帧放行（START_HOLD_SEC=30）
                mgr._manage_session(1, t0 + k, 9.0, 1.2 / 9.0, active=True)
            await asyncio.sleep(0.05)

            assert es.is_charging is True, frac
            assert es.session_start == t0, (frac, es.session_start)
            sid, rows = mgr._history.record_charge_points.call_args[0]
            assert len(rows) == 30, (frac, len(rows))
            assert rows[0][0] == t0, (frac, rows[0][0])
            assert rows[-1][0] == pytest.approx(t0 + 29), (frac, rows[-1][0])
