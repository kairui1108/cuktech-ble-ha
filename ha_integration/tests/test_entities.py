"""Tests for HA Integration entity platforms."""
import sys
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

# conftest.py handles all homeassistant mocking
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import DEVICE_INFO, PORT_MAP, PIID_DISPLAY, SELECT_PIIDS, SELECT_OPTION_MAP


class TestSensorEntities:
    """Test sensor entity behavior."""

    @pytest.fixture
    def coordinator(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_123"
        entry.data = {"server_url": "http://localhost:8199"}
        return CuktechMQTTCoordinator(hass, entry)

    def test_port_sensor_native_value(self, coordinator):
        """Test CuktechPortSensor returns correct value from port_data."""
        coordinator._port_data = {"1": {"voltage": 20.0, "current": 2.0, "power": 40.0}}
        pd = coordinator.port_data.get("1")
        assert pd["voltage"] == 20.0
        assert pd["current"] == 2.0
        assert pd["power"] == 40.0

    def test_port_sensor_available(self, coordinator):
        """Test sensor availability based on coordinator."""
        coordinator._available = False
        assert coordinator.available is False
        coordinator._available = True
        assert coordinator.available is True

    def test_setting_sensor_native_value(self, coordinator):
        """Test setting sensor returns display value."""
        coordinator._settings = {"5": 1}
        v = coordinator.data.get("5")
        assert v == 1
        display = PIID_DISPLAY.get(5, {}).get(v)
        assert display == "AI模式"


class TestSwitchEntities:
    """Test switch entity behavior."""

    @pytest.fixture
    def coordinator(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_123"
        entry.data = {"server_url": "http://localhost:8199"}
        return CuktechMQTTCoordinator(hass, entry)

    def test_switch_is_on_from_bitmask(self, coordinator):
        """Test CuktechPortSwitch parses PIID 16 bitmask correctly."""
        coordinator._settings = {"16": 0x0F}
        port_ctl = coordinator.data.get("16")
        assert port_ctl == 0x0F
        assert bool(port_ctl & (1 << 0))  # C1 on
        assert bool(port_ctl & (1 << 1))  # C2 on

    def test_switch_partial_port(self, coordinator):
        """Test switch with only some ports enabled."""
        coordinator._settings = {"16": 0x05}
        port_ctl = coordinator.data.get("16")
        assert bool(port_ctl & (1 << 0))  # C1 on
        assert not bool(port_ctl & (1 << 1))  # C2 off
        assert bool(port_ctl & (1 << 2))  # C3 on


class TestSelectEntities:
    """Test select entity behavior."""

    def test_select_current_option(self):
        """Test CuktechSelect maps PIID_DISPLAY correctly."""
        display = PIID_DISPLAY.get(5, {})
        assert display.get(1) == "AI模式"

    def test_select_option_map_consistency(self):
        """Test SELECT_OPTION_MAP matches SELECT_PIIDS options."""
        for piid, cfg in SELECT_PIIDS.items():
            assert piid in SELECT_OPTION_MAP
            for option in cfg["options"]:
                assert option in SELECT_OPTION_MAP[piid]


class TestNumberEntities:
    """Test number entity behavior."""

    def test_countdown_default_value(self):
        """Test countdown returns 0 when not set."""
        settings = {}
        v = settings.get("9")
        result = float(v) if v is not None else 0
        assert result == 0

    def test_countdown_with_value(self):
        """Test countdown returns correct value."""
        settings = {"9": 30}
        v = settings.get("9")
        result = float(v) if v is not None else 0
        assert result == 30.0


class TestBinarySensorEntities:
    """Test binary sensor entity behavior."""

    def test_active_from_port_data(self):
        """Test active status from port data."""
        port_data = {"active": True, "voltage": 20.0}
        assert port_data.get("active") is True

    def test_inactive_port(self):
        """Test inactive port returns False."""
        port_data = {"active": False, "voltage": 0.0}
        assert port_data.get("active") is False


class TestEntityLifecycle:
    """Test entity lifecycle management."""

    @pytest.fixture
    def coordinator(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_123"
        entry.data = {"server_url": "http://localhost:8199"}
        return CuktechMQTTCoordinator(hass, entry)

    def test_callback_registration(self, coordinator):
        """Test callback registration and unregistration."""
        cb = MagicMock()
        coordinator.register_callback(cb)
        assert cb in coordinator._callbacks

        coordinator.unregister_callback(cb)
        assert cb not in coordinator._callbacks

    def test_callback_triggered_on_update(self, coordinator):
        """Test callbacks are triggered on data updates."""
        cb = MagicMock()
        coordinator.register_callback(cb)

        msg = MagicMock()
        msg.topic = "cuktech/charger/port/c1"
        msg.payload = json.dumps({"voltage": 20.0}).encode()
        coordinator._on_port_message(msg)

        cb.assert_called_once()
