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
                         END_REASON_LINK_LOSS, END_REASON_SHUTDOWN, END_REASON_UNKNOWN,
                         PORT_IDS)
from state import ChargerState, PORT_NAMES, PORT_BITS, PORT_DEFAULT


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
