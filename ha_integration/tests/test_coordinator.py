"""Tests for HA Integration MQTT Coordinator."""
import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

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

    def test_callback_registration(self, coordinator):
        """Test callback registration and unregistration."""
        callback = MagicMock()
        coordinator.register_callback(callback)
        assert callback in coordinator._callbacks

        coordinator.unregister_callback(callback)
        assert callback not in coordinator._callbacks

    def test_callback_limit(self, coordinator, caplog):
        """Test callback limit warning is logged."""
        import logging
        with caplog.at_level(logging.WARNING):
            for _ in range(105):
                coordinator.register_callback(MagicMock())
        assert len(coordinator._callbacks) == 105
        assert any("Too many callbacks" in msg for msg in caplog.messages)

    def test_health_failures_not_reset_by_availability(self, coordinator):
        """Test health failures counter is NOT reset by _update_availability alone."""
        coordinator._health_failures = 5
        coordinator._mqtt_connected = True
        coordinator._update_availability()
        assert coordinator._health_failures == 5  # Only reset on MQTT reconnect, only in _on_status_message

    def test_update_availability_mqtt(self, coordinator):
        """Test availability update with MQTT connected."""
        coordinator._mqtt_connected = True
        coordinator._last_status_time = time.time()
        coordinator._update_availability()
        assert coordinator.available is True

    def test_update_availability_http_recent(self, coordinator):
        """Test availability update with recent HTTP check."""
        coordinator._mqtt_connected = False
        coordinator._last_status_time = time.time()
        coordinator._update_availability()
        assert coordinator.available is True

    def test_update_availability_stale(self, coordinator):
        """Test availability update with stale HTTP check."""
        coordinator._mqtt_connected = False
        coordinator._last_status_time = time.time() - 60
        coordinator._update_availability()
        assert coordinator.available is False

    def test_data_returns_copy(self, coordinator):
        """Test that data property returns a copy."""
        coordinator._settings = {"5": 1, "6": 0}
        data1 = coordinator.data
        data2 = coordinator.data
        assert data1 == data2
        assert data1 is not data2

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
        coordinator._last_status_time = time.time() - 60

        # Simulate MQTT reconnect
        msg = MagicMock()
        msg.topic = "cuktech/charger/status"
        msg.payload = json.dumps({"connected": True}).encode()
        coordinator._on_status_message(msg)

        assert coordinator._health_failures == 0
        assert coordinator._mqtt_connected is True

    @pytest.mark.asyncio
    async def test_async_health_check_success(self, coordinator):
        """Test HTTP health check succeeds."""
        from unittest.mock import MagicMock, patch
        from types import SimpleNamespace

        session = MagicMock()
        session.get = MagicMock(return_value=_AsyncContextManager(SimpleNamespace(status=200)))

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
        from types import SimpleNamespace

        session = MagicMock()
        session.get = MagicMock(return_value=_AsyncContextManager(SimpleNamespace(status=503)))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is False

    def test_on_port_message_malformed_json(self, coordinator):
        """Test _on_port_message handles malformed JSON gracefully."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = b"not json"

        # Should not raise
        coordinator._on_port_message(msg)
        assert coordinator._port_data == {}

    def test_on_port_message_empty_payload(self, coordinator):
        """Test _on_port_message handles empty payload - stores empty dict."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = b"{}"

        coordinator._on_port_message(msg)
        # Empty JSON is valid, stores {} for port 1
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

        # Should not raise
        coordinator._on_status_message(msg)

    def test_on_status_message_connected_false(self, coordinator):
        """Test _on_status_message with connected=False sets _mqtt_connected and device becomes unavailable."""
        msg = MagicMock()
        msg.topic = "cuktech/charger/status"
        msg.payload = json.dumps({"connected": False}).encode()

        coordinator._on_status_message(msg)
        assert coordinator._mqtt_connected is False
        # _last_status_time is NOT updated when connected: false, so device is unavailable
        assert coordinator.available is False

    @pytest.mark.asyncio
    async def test_async_set_value(self, coordinator):
        """Test async_set_value publishes MQTT command with correct topic/payload."""
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
        """Test async_port_control publishes MQTT command with correct topic/payload."""
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

    def test_notify_callbacks(self, coordinator):
        """Test _notify_callbacks calls all registered callbacks."""
        cb1 = MagicMock()
        cb2 = MagicMock()
        coordinator.register_callback(cb1)
        coordinator.register_callback(cb2)
        coordinator._notify_callbacks()
        cb1.assert_called_once()
        cb2.assert_called_once()

    def test_port_data_property(self, coordinator):
        """Test port_data property returns _port_data."""
        coordinator._port_data = {"1": {"voltage": 20.0}}
        assert coordinator.port_data == {"1": {"voltage": 20.0}}
