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

    def test_callback_limit(self, coordinator):
        """Test callback limit warning."""
        for _ in range(101):
            coordinator.register_callback(MagicMock())
        assert len(coordinator._callbacks) == 101
        # Warning should be logged (verify via logger mock if needed)

    def test_health_failures_reset(self, coordinator):
        """Test health failures counter reset."""
        coordinator._health_failures = 5
        coordinator._mqtt_connected = True
        coordinator._update_availability()
        assert coordinator._health_failures == 5  # Not reset here, only in _on_status_message

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
        from unittest.mock import AsyncMock, patch

        resp = AsyncMock()
        resp.status = 200
        session = AsyncMock()
        session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock()))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is True
        assert coordinator._health_failures == 0

    @pytest.mark.asyncio
    async def test_async_health_check_failure(self, coordinator):
        """Test HTTP health check handles failure."""
        from unittest.mock import AsyncMock, patch

        session = AsyncMock()
        session.get = AsyncMock(side_effect=Exception("Timeout"))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is False
        assert coordinator._health_failures == 1

    @pytest.mark.asyncio
    async def test_async_health_check_bad_status(self, coordinator):
        """Test HTTP health check handles bad status code."""
        from unittest.mock import AsyncMock, patch

        resp = AsyncMock()
        resp.status = 503
        session = AsyncMock()
        session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock()))

        with patch('custom_components.cuktech_charger.async_get_clientsession', return_value=session):
            await coordinator._async_health_check(None)

        assert coordinator._available is False
