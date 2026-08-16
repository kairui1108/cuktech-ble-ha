"""Tests for ble_manager.py - BLE connection manager."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ble_manager import BLEManager, set_status_cache_invalidator, _invalidate
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
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"ok": True})

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


class TestHandleMultiframe:
    """Test multi-frame data handling."""

    @pytest.mark.asyncio
    async def test_multiframe_large_count_sends_ack(self):
        """Test multiframe with frame_count > 1000 sends ACK and consumes all frames."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        # Decrypt-failure recovery shouldn't interfere with the drain loop test:
        # stub out inline processing so the drain only exercises wait_notify.
        mgr._try_process_inline_frame = AsyncMock()
        call_count = 0
        async def fake_wait_notify(name, timeout=5.0):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                raise asyncio.TimeoutError()
            return bytes(20)
        mgr.ctrl.wait_notify = fake_wait_notify

        # data[2]=0x00 triggers multiframe branch, frame_count=0x03e9=1001 > 1000
        data = bytes([0, 0, 0x00, 4, 0x03, 0xe9])

        await mgr._handle_multiframe(data)
        assert mgr.ctrl.client.write_gatt_char.call_count == 2
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
    async def test_multiframe_large_count(self):
        """Test multiframe with frame_count=1001 drains frames."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.client = MagicMock()
        mgr.ctrl.client.write_gatt_char = AsyncMock()
        # Stub inline processing so decrypt-failure recovery doesn't abort the drain.
        mgr._try_process_inline_frame = AsyncMock()
        call_count = 0

        async def fake_wait_notify(name, timeout=5.0):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                raise asyncio.TimeoutError()
            return bytes(20)

        mgr.ctrl.wait_notify = fake_wait_notify

        # frame_count = 0x03e9 = 1001
        data = bytes([0, 0, 0x00, 4, 0x03, 0xe9])

        await mgr._handle_multiframe(data)

        # ACK + drain loop hit 5 times before timeout + final ACK
        assert mgr.ctrl.client.write_gatt_char.call_count == 2
        assert call_count == 6


class TestConcurrency:
    """Test concurrent command processing."""

    @pytest.mark.asyncio
    async def test_concurrent_commands(self):
        """Test multiple commands in queue are all processed."""
        mgr = make_manager()
        mgr.ctrl = MagicMock()
        mgr.ctrl.send_miot_command = AsyncMock(return_value={"ok": True})
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
