"""Tests for HA Integration MQTT Coordinator."""
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# conftest.py already mocks homeassistant modules
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger import CuktechMQTTCoordinator


class _AsyncContextManager:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self._resp
    async def __aexit__(self, *args):
        return False


class TestCuktechMQTTCoordinator:
    """Test CuktechMQTTCoordinator."""

    @pytest.fixture
    def coordinator(self, mock_hass, mock_entry):
        """Create a coordinator instance."""
        return CuktechMQTTCoordinator(mock_hass, mock_entry)

    def test_initial_state(self, coordinator):
        """Test initial coordinator state."""
        assert coordinator.available is False
        assert coordinator._mqtt_connected is False
        assert coordinator._health_failures == 0
        assert coordinator.port_data == {}
        assert coordinator.data == {}

    # --- Callback tests ---

    def test_callback_registration(self, coordinator):
        """Test generic callback registration and unregistration."""
        cb = MagicMock()
        coordinator.register_callback(cb)
        assert cb in coordinator._callbacks
        coordinator.unregister_callback(cb)
        assert cb not in coordinator._callbacks

    def test_port_callback_registration(self, coordinator):
        """Test port callback registration and unregistration."""
        cb = MagicMock()
        coordinator.register_port_callback(cb)
        assert cb in coordinator._port_callbacks
        coordinator.unregister_port_callback(cb)
        assert cb not in coordinator._port_callbacks

    def test_settings_callback_registration(self, coordinator):
        """Test settings callback registration and unregistration."""
        cb = MagicMock()
        coordinator.register_settings_callback(cb)
        assert cb in coordinator._settings_callbacks
        coordinator.unregister_settings_callback(cb)
        assert cb not in coordinator._settings_callbacks

    def test_notify_callbacks(self, coordinator):
        """Test _notify_callbacks calls all registered callbacks."""
        cb1 = MagicMock()
        cb2 = MagicMock()
        coordinator.register_callback(cb1)
        coordinator.register_callback(cb2)
        # Call with no args (legacy path — uses self._callbacks)
        coordinator._notify_callbacks()
        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_notify_specific_list(self, coordinator):
        """Test _notify_callbacks with a specific list."""
        cb1 = MagicMock()
        cb2 = MagicMock()
        coordinator._port_callbacks.append(cb1)
        coordinator._callbacks.append(cb2)
        coordinator._notify_callbacks(coordinator._port_callbacks)
        cb1.assert_called_once()
        cb2.assert_not_called()

    def test_notify_callbacks_exception_handling(self, coordinator):
        """Test _notify_callbacks handles exceptions from callbacks gracefully."""
        good_cb = MagicMock()
        bad_cb = MagicMock(side_effect=Exception("test error"))
        coordinator.register_callback(good_cb)
        coordinator.register_callback(bad_cb)
        coordinator._notify_callbacks()
        good_cb.assert_called_once()
        bad_cb.assert_called_once()

    # --- Availability tests ---

    def test_health_failures_not_reset_by_availability(self, coordinator):
        """Test health failures counter is NOT reset by _update_availability alone."""
        coordinator._health_failures = 5
        coordinator._mqtt_connected = True
        coordinator._update_availability()
        assert coordinator._health_failures == 5

    def test_update_availability_mqtt(self, coordinator):
        """Test availability update with MQTT connected."""
        coordinator._mqtt_connected = True
        coordinator._last_status_time = 980
        coordinator._update_availability()
        assert coordinator.available is True

    def test_update_availability_http_recent(self, coordinator):
        """Test availability update with recent HTTP check."""
        coordinator._mqtt_connected = False
        coordinator._last_status_time = 980
        coordinator._update_availability()
        assert coordinator.available is True

    def test_update_availability_stale(self, coordinator):
        """Test availability update with stale HTTP check."""
        coordinator._mqtt_connected = False
        coordinator._last_status_time = 900
        coordinator._update_availability()
        assert coordinator.available is False

    def test_data_returns_copy(self, coordinator):
        """Test that data property returns a copy."""
        coordinator._settings = {"5": 1, "6": 0}
        data1 = coordinator.data
        data2 = coordinator.data
        assert data1 == data2
        assert data1 is not data2

    # --- MQTT message handler tests ---

    def test_on_port_message_parsing(self, coordinator):
        """Test MQTT port message parsing."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = json.dumps({"voltage": 20.0, "current": 2.0, "power": 40.0}).encode()

        coordinator._on_port_message(msg)
        assert coordinator._port_data["1"]["voltage"] == 20.0
        assert coordinator._port_data["1"]["current"] == 2.0

    def test_on_settings_message_parsing(self, coordinator):
        """Test MQTT settings message parsing."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/settings"
        msg.payload = json.dumps({"5": 1, "6": 0}).encode()

        coordinator._on_settings_message(msg)
        assert coordinator._settings == {"5": 1, "6": 0}

    def test_health_failures_reset_on_success(self, coordinator):
        """Test health failures counter reset on MQTT reconnect."""
        coordinator._health_failures = 5
        coordinator._mqtt_connected = False
        coordinator._last_status_time = 900

        msg = MagicMock()
        msg.topic = "cuktech/charger/status"
        msg.payload = json.dumps({"connected": True}).encode()
        coordinator._on_status_message(msg)

        assert coordinator._health_failures == 0
        assert coordinator._mqtt_connected is True

    def test_on_port_message_malformed_json(self, coordinator):
        """Test _on_port_message handles malformed JSON gracefully."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = b"not json"

        coordinator._on_port_message(msg)
        assert coordinator._port_data == {}

    def test_on_port_message_empty_payload(self, coordinator):
        """Test _on_port_message handles empty payload - stores empty dict."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = b"{}"

        coordinator._on_port_message(msg)
        assert coordinator._port_data == {"1": {}}

    def test_on_port_message_unknown_topic(self, coordinator):
        """Test _on_port_message ignores unknown topics."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/unknown"
        msg.payload = b'{"voltage": 20.0}'

        coordinator._on_port_message(msg)
        assert len(coordinator._port_data) == 0

    def test_on_settings_message_malformed_json(self, coordinator):
        """Test _on_settings_message handles malformed JSON gracefully."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/settings"
        msg.payload = b"not json"

        coordinator._on_settings_message(msg)
        assert coordinator._settings == {}

    def test_on_status_message_malformed_json(self, coordinator):
        """Test _on_status_message handles malformed JSON gracefully."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/status"
        msg.payload = b"not json"

        coordinator._on_status_message(msg)

    def test_on_status_message_connected_false(self, coordinator):
        """Test _on_status_message with connected=False."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/status"
        msg.payload = json.dumps({"connected": False}).encode()

        coordinator._on_status_message(msg)
        assert coordinator._mqtt_connected is False
        assert coordinator.available is False

    # --- Device info sync tests ---

    def test_sync_device_info_from_payload(self, coordinator):
        """Test _sync_device_info_from_payload updates model and firmware."""
        changed = coordinator._sync_device_info_from_payload({
            "device_model": "test-model",
            "firmware_version": "v1.2.3",
        })
        assert changed is True
        assert coordinator._device_model == "test-model"
        assert coordinator._firmware_version == "v1.2.3"

    def test_sync_device_info_from_payload_no_change(self, coordinator):
        """Test _sync_device_info_from_payload returns False when nothing changes."""
        coordinator._device_model = "existing"
        coordinator._firmware_version = "v1.0"
        changed = coordinator._sync_device_info_from_payload({
            "device_model": "existing",
            "firmware_version": "v1.0",
        })
        assert changed is False

    def test_sync_ble_state_connected(self, coordinator):
        """Test _sync_ble_state when BLE connects."""
        coordinator._ble_enabled = False
        result = coordinator._sync_ble_state(True)
        assert result is True
        assert coordinator._ble_connected is True
        assert coordinator._ble_enabled is True

    def test_sync_ble_state_disconnected(self, coordinator):
        """Test _sync_ble_state when BLE disconnects."""
        coordinator._ble_enabled = True
        result = coordinator._sync_ble_state(False)
        assert result is True
        assert coordinator._ble_connected is False
        assert coordinator._ble_enabled is False

    def test_sync_ble_state_disconnect_clears_port_data(self, coordinator):
        """BLE 断连（已连接→断开）时应清空端口数据，避免展示陈旧读数。"""
        coordinator._ble_enabled = True
        coordinator._ble_connected = True
        coordinator._port_data = {"1": {"voltage": 20.0}, "2": {"power": 40.0}}
        cb = MagicMock()
        coordinator.register_port_callback(cb)
        result = coordinator._sync_ble_state(False)
        assert result is True
        assert coordinator._ble_connected is False
        assert coordinator._port_data == {}
        cb.assert_called_once()

    def test_sync_ble_state_disconnect_noop_when_never_connected(self, coordinator):
        """从未连接过时断连不应误清数据（初始状态 _ble_connected=False）。"""
        coordinator._port_data = {"1": {"voltage": 20.0}}
        result = coordinator._sync_ble_state(False)
        assert result is False
        assert coordinator._port_data == {"1": {"voltage": 20.0}}  # 无变化

    # --- MQTT readiness tests ---

    @pytest.mark.asyncio
    async def test_wait_mqtt_ready_success(self, coordinator):
        """MQTT 客户端可用时 _async_wait_mqtt_ready 正常返回。"""
        from unittest.mock import patch, AsyncMock
        with patch('custom_components.cuktech_charger.mqtt.async_wait_for_mqtt_client',
                   new=AsyncMock(return_value=True)):
            await coordinator._async_wait_mqtt_ready()  # 不应抛出

    @pytest.mark.asyncio
    async def test_wait_mqtt_ready_failure(self, coordinator):
        """MQTT 不可用时抛出 ConfigEntryNotReady，交给 HA 稍后重试 setup。"""
        from unittest.mock import patch, AsyncMock
        from homeassistant.exceptions import ConfigEntryNotReady
        with patch('custom_components.cuktech_charger.mqtt.async_wait_for_mqtt_client',
                   new=AsyncMock(return_value=False)):
            with pytest.raises(ConfigEntryNotReady):
                await coordinator._async_wait_mqtt_ready()

    # --- HTTP health check tests ---

    @pytest.mark.asyncio
    async def test_async_health_check_success(self, coordinator):
        """Test HTTP health check succeeds."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"connected": True, "firmware_version": "v1.0.0"})

        session = MagicMock()
        session.get = MagicMock(return_value=_AsyncContextManager(mock_resp))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is True
        assert coordinator._health_failures == 0

    @pytest.mark.asyncio
    async def test_async_health_check_failure(self, coordinator):
        """Test HTTP health check handles failure."""
        from unittest.mock import MagicMock, patch

        session = MagicMock()
        session.get = MagicMock(side_effect=Exception("Timeout"))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is False
        assert coordinator._health_failures == 1

    @pytest.mark.asyncio
    async def test_async_health_check_bad_status(self, coordinator):
        """Test HTTP health check handles bad status code."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.status = 503

        session = MagicMock()
        session.get = MagicMock(return_value=_AsyncContextManager(mock_resp))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is False

    @pytest.mark.asyncio
    async def test_async_health_check_parse_body_error(self, coordinator):
        """Test health check handles JSON parse failure gracefully."""
        from unittest.mock import MagicMock, patch

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=Exception("Parse error"))

        session = MagicMock()
        session.get = MagicMock(return_value=_AsyncContextManager(mock_resp))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        # Should not crash, available should be True (status was 200)
        assert coordinator._available is True

    # --- Command tests ---

    @pytest.mark.asyncio
    async def test_async_set_value(self, coordinator):
        """Test async_set_value publishes MQTT command."""
        from unittest.mock import patch, AsyncMock
        with patch('custom_components.cuktech_charger.mqtt') as mock_mqtt:
            mock_mqtt.async_publish = AsyncMock()
            await coordinator.async_set_value(5, 1)
            mock_mqtt.async_publish.assert_called_once()
            call_args = mock_mqtt.async_publish.call_args
            topic = call_args[0][1]
            assert "set" in topic
            payload = json.loads(call_args[0][2])
            assert payload["piid"] == 5
            assert payload["value"] == 1

    @pytest.mark.asyncio
    async def test_async_port_control(self, coordinator):
        """Test async_port_control publishes MQTT command."""
        from unittest.mock import patch, AsyncMock
        with patch('custom_components.cuktech_charger.mqtt') as mock_mqtt:
            mock_mqtt.async_publish = AsyncMock()
            await coordinator.async_port_control("c1", "on")
            mock_mqtt.async_publish.assert_called_once()
            call_args = mock_mqtt.async_publish.call_args
            topic = call_args[0][1]
            assert "port" in topic
            payload = json.loads(call_args[0][2])
            assert payload["port"] == "c1"
            assert payload["action"] == "on"

    # --- Properties ---

    def test_port_data_property(self, coordinator):
        """Test port_data property returns _port_data."""
        coordinator._port_data = {"1": {"voltage": 20.0}}
        assert coordinator.port_data == {"1": {"voltage": 20.0}}

    def test_protocol_switches_default(self, coordinator):
        """Test protocol_switches returns all False when no setting."""
        sw = coordinator.protocol_switches
        for port in ["c1", "c2", "c3", "a"]:
            for proto_val in sw[port].values():
                assert proto_val is False

    def test_protocol_switches_decoding(self, coordinator):
        """Test protocol_switches decodes PIID 21 correctly."""
        coordinator._settings = {"21": 0x0201080F}
        sw = coordinator.protocol_switches
        assert sw["c1"]["pd"] is True
        assert sw["c1"]["pps"] is True
        assert sw["c1"]["ufcs"] is True
        assert sw["c2"]["pd"] is False
        assert sw["c2"]["pps"] is False
        assert sw["c2"]["ufcs"] is False
        assert sw["c3"]["ufcs"] is True
        assert sw["c3"]["scp"] is False
        assert sw["a"]["scp"] is True
        assert sw["a"]["ufcs"] is False

    def test_encode_protocol_extend_all_on(self, coordinator):
        """Test _encode_protocol_extend: all ON."""
        switches = {
            "c1": {"pd": True, "pps": True, "ufcs": True},
            "c2": {"pd": True, "pps": True, "ufcs": True},
            "c3": {"ufcs": True, "scp": True},
            "a":  {"ufcs": True, "scp": True},
        }
        result = coordinator._encode_protocol_extend(switches)
        assert result == 0x03030F0F

    def test_encode_protocol_extend_single(self, coordinator):
        """Test _encode_protocol_extend: single protocol."""
        switches = {
            "c1": {"pd": True, "pps": False, "ufcs": False},
            "c2": {"pd": False, "pps": False, "ufcs": False},
            "c3": {"ufcs": False, "scp": False},
            "a":  {"ufcs": False, "scp": False},
        }
        result = coordinator._encode_protocol_extend(switches)
        assert result == 0x00000809

    def test_protocol_switches_roundtrip(self, coordinator):
        """Test encode then decode roundtrip."""
        original = {
            "c1": {"pd": True, "pps": False, "ufcs": True},
            "c2": {"pd": False, "pps": True, "ufcs": False},
            "c3": {"ufcs": True, "scp": False},
            "a":  {"ufcs": False, "scp": True},
        }
        encoded = coordinator._encode_protocol_extend(original)
        coordinator._settings = {"21": encoded}
        decoded = coordinator.protocol_switches
        for port in ["c1", "c2", "c3", "a"]:
            for proto in original[port]:
                assert decoded[port][proto] == original[port][proto]

    def test_protocol_switches_no_data(self, coordinator):
        """Test protocol_switches returns all False when settings has no 21."""
        coordinator._settings = {"5": 1}
        sw = coordinator.protocol_switches
        for port in ["c1", "c2", "c3", "a"]:
            for proto_val in sw[port].values():
                assert proto_val is False

    @pytest.mark.asyncio
    async def test_async_set_protocol(self, coordinator):
        """Test async_set_protocol publishes encoded value."""
        from unittest.mock import patch, AsyncMock
        coordinator._settings = {"21": 0x03030F0F}  # all ON
        with patch('custom_components.cuktech_charger.mqtt') as mock_mqtt:
            mock_mqtt.async_publish = AsyncMock()
            await coordinator.async_set_protocol("c1", "pd", False)
            mock_mqtt.async_publish.assert_called_once()
            call_args = mock_mqtt.async_publish.call_args
            payload = json.loads(call_args[0][2])
            assert payload["piid"] == 21
            assert payload["value"] == 0x03030F0E

    @pytest.mark.asyncio
    async def test_async_set_protocol_unknown(self, coordinator):
        """Test async_set_protocol with unknown port/protocol does nothing."""
        from unittest.mock import patch, MagicMock
        coord = coordinator
        coord._settings = {"21": 0}
        with patch('custom_components.cuktech_charger.mqtt') as mock_mqtt:
            mock_mqtt.async_publish = MagicMock()
            await coord.async_set_protocol("invalid", "pd", True)
            mock_mqtt.async_publish.assert_not_called()
            assert coord._settings["21"] == 0
