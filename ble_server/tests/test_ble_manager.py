"""Tests for ble_manager.py - BLE connection manager."""
import asyncio
import sys
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
    """Test exponential backoff delay calculation."""

    def test_initial_delay(self):
        """Test initial delay is base delay."""
        mgr = make_manager()
        mgr._reconnect_attempts = 0
        assert mgr._get_reconnect_delay() == 1.0

    def test_exponential_increase(self):
        """Test delay increases exponentially."""
        mgr = make_manager()
        mgr._reconnect_attempts = 3
        assert mgr._get_reconnect_delay() == 8.0

    def test_max_delay_cap(self):
        """Test delay is capped at max."""
        mgr = make_manager()
        mgr._reconnect_attempts = 10
        assert mgr._get_reconnect_delay() == 300.0

    def test_attempts_capped(self):
        """Test attempts are capped at 10 for exponent."""
        mgr = make_manager()
        mgr._reconnect_attempts = 100
        assert mgr._get_reconnect_delay() == 300.0


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
        """Test _decrypt_failures increments on repeated decrypt failures."""
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

        await mgr._handle_inline_data(data)
        assert mgr._decrypt_failures == 3

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
