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
        publisher.assert_called_once_with("cuktech/charger/port/c1", data)

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
