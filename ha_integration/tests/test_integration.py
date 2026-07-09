"""Tests for HA Integration entity lifecycle and coordinator integration."""
import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

# conftest.py handles all homeassistant mocking
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import DEVICE_INFO, PORT_MAP


class TestCoordinatorIntegration:
    """Test coordinator integration with entity lifecycle."""

    @pytest.fixture
    def coordinator(self):
        """Create a coordinator instance."""
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry_123"
        entry.data = {"server_url": "http://localhost:8199"}
        return CuktechMQTTCoordinator(hass, entry)

    def test_callback_lifecycle(self, coordinator):
        """Test callback registration and unregistration."""
        callbacks = []
        for _ in range(5):
            cb = MagicMock()
            coordinator.register_callback(cb)
            callbacks.append(cb)

        assert len(coordinator._callbacks) == 5

        for cb in callbacks[:3]:
            coordinator.unregister_callback(cb)

        assert len(coordinator._callbacks) == 2

    def test_port_data_updates(self, coordinator):
        """Test that port data updates trigger callbacks."""
        callback = MagicMock()
        coordinator.register_callback(callback)

        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = json.dumps({"voltage": 20.0, "current": 2.0, "power": 40.0}).encode()

        coordinator._on_port_message(msg)
        callback.assert_called_once()

    def test_settings_updates(self, coordinator):
        """Test that settings updates trigger callbacks."""
        callback = MagicMock()
        coordinator.register_callback(callback)

        msg = MagicMock()
        msg.topic = "cuktech/charger/settings"
        msg.payload = json.dumps({"5": 1, "6": 0}).encode()

        coordinator._on_settings_message(msg)
        callback.assert_called_once()

    def test_availability_transitions(self, coordinator):
        """Test availability state transitions."""
        # Initially unavailable
        assert coordinator.available is False

        # MQTT connected
        coordinator._mqtt_connected = True
        coordinator._last_status_time = time.time()
        coordinator._update_availability()
        assert coordinator.available is True

        # MQTT disconnected but HTTP recent
        coordinator._mqtt_connected = False
        coordinator._last_status_time = time.time()
        coordinator._update_availability()
        assert coordinator.available is True

        # MQTT disconnected and HTTP stale
        coordinator._last_status_time = time.time() - 60
        coordinator._update_availability()
        assert coordinator.available is False

    def test_device_info(self, coordinator):
        """Test device info is consistent."""
        assert DEVICE_INFO["name"] == "CUKTECH Charger"
        assert DEVICE_INFO["manufacturer"] == "CUKTECH"
        assert DEVICE_INFO["model"] == "10 GaN Charger Ultra"

    def test_port_map_complete(self):
        """Test port map has all required ports."""
        assert len(PORT_MAP) == 4
        assert all(v in range(1, 5) for v in PORT_MAP.values())
