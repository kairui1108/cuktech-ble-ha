"""Tests for HA Integration entity platforms."""
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger import CuktechMQTTCoordinator
from custom_components.cuktech_charger.const import PORT_MAP, PIID_DISPLAY, SELECT_PIIDS, SELECT_OPTION_MAP


class TestSensorEntities:
    """Test sensor entity behavior using real Entity classes."""

    def test_port_sensor_native_value(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {"1": {"voltage": 20.0, "current": 2.0, "power": 40.0}}
        sensor = CuktechPortSensor(coord, mock_entry, 1, "c1", "voltage")
        assert sensor.native_value == 20.0

    def test_port_sensor_current(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {"1": {"voltage": 20.0, "current": 2.5, "power": 50.0}}
        sensor = CuktechPortSensor(coord, mock_entry, 1, "c1", "current")
        assert sensor.native_value == 2.5

    def test_port_sensor_power(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {"1": {"voltage": 20.0, "current": 2.0, "power": 40.0}}
        sensor = CuktechPortSensor(coord, mock_entry, 1, "c1", "power")
        assert sensor.native_value == 40.0

    def test_port_sensor_no_data(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {}
        sensor = CuktechPortSensor(coord, mock_entry, 1, "c1", "voltage")
        assert sensor.native_value is None

    def test_setting_sensor_native_value(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.select import CuktechSelect
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"5": 1}
        select = CuktechSelect(coord, mock_entry, 5, "scene", "mdi:cog", ["AI模式", "数码生态", "单口模式", "均衡模式"])
        assert select.current_option == "AI模式"

    def test_setting_sensor_unknown_value(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.select import CuktechSelect
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"5": 99}
        select = CuktechSelect(coord, mock_entry, 5, "scene", "mdi:cog", ["AI模式", "数码生态", "单口模式", "均衡模式"])
        assert select.current_option is None

    def test_total_power_sensor(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechTotalPowerSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {
            "1": {"power": 20.0, "active": True},
            "2": {"power": 30.0, "active": True},
        }
        sensor = CuktechTotalPowerSensor(coord, mock_entry)
        assert sensor.native_value == 50.0

    def test_total_power_inactive_ports(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechTotalPowerSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {
            "1": {"power": 20.0, "active": False},
            "2": {"power": 30.0, "active": True},
        }
        sensor = CuktechTotalPowerSensor(coord, mock_entry)
        assert sensor.native_value == 30.0

    def test_sensor_available(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.sensor import CuktechPortSensor
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        sensor = CuktechPortSensor(coord, mock_entry, 1, "c1", "voltage")
        coord._available = False
        assert sensor.available is False
        coord._available = True
        assert sensor.available is True


class TestSwitchEntities:
    """Test switch entity behavior using real Entity classes."""

    def test_port_switch_is_on(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.switch import CuktechPortSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"16": 0x0F}
        switch = CuktechPortSwitch(coord, mock_entry, "c1", "C1", "mdi:usb-c-port", 0)
        switch.hass = MagicMock()
        assert switch.is_on is True

    def test_port_switch_off(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.switch import CuktechPortSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"16": 0x00}
        switch = CuktechPortSwitch(coord, mock_entry, "c1", "C1", "mdi:usb-c-port", 0)
        switch.hass = MagicMock()
        assert switch.is_on is False

    def test_port_switch_individual_bits(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.switch import CuktechPortSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"16": 0x05}
        c1 = CuktechPortSwitch(coord, mock_entry, "c1", "C1", "mdi:usb", 0)
        c1.hass = MagicMock()
        assert c1.is_on is True
        c2 = CuktechPortSwitch(coord, mock_entry, "c2", "C2", "mdi:usb", 1)
        c2.hass = MagicMock()
        assert c2.is_on is False

    def test_setting_switch(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.switch import CuktechSettingSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"15": 1}
        switch = CuktechSettingSwitch(coord, mock_entry, 15, "USB-A", "mdi:usb")
        switch.hass = MagicMock()
        assert switch.is_on is True

    def test_setting_switch_off(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.switch import CuktechSettingSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"15": 0}
        switch = CuktechSettingSwitch(coord, mock_entry, 15, "USB-A", "mdi:usb")
        switch.hass = MagicMock()
        assert switch.is_on is False


class TestBinarySensorEntities:
    """Test binary sensor entity behavior."""

    def test_active(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.binary_sensor import CuktechPortActive
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {"1": {"active": True}}
        bs = CuktechPortActive(coord, mock_entry, 1, "c1")
        assert bs.is_on is True

    def test_inactive(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.binary_sensor import CuktechPortActive
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {"1": {"active": False}}
        bs = CuktechPortActive(coord, mock_entry, 1, "c1")
        assert bs.is_on is False

    def test_no_data(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.binary_sensor import CuktechPortActive
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._port_data = {}
        bs = CuktechPortActive(coord, mock_entry, 1, "c1")
        assert bs.is_on is None


class TestSelectEntities:
    """Test select entity behavior."""

    def test_select_current_option(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.select import CuktechSelect
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"5": 2}
        sel = CuktechSelect(coord, mock_entry, 5, "scene", "mdi:cog", ["AI模式", "数码生态", "单口模式", "均衡模式"])
        assert sel.current_option == "数码生态"

    def test_select_no_data(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.select import CuktechSelect
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {}
        sel = CuktechSelect(coord, mock_entry, 5, "scene", "mdi:cog", ["AI模式", "数码生态", "单口模式", "均衡模式"])
        assert sel.current_option is None

    def test_select_options_populated(self):
        for piid, cfg in SELECT_PIIDS.items():
            assert piid in SELECT_OPTION_MAP
            for option in cfg["options"]:
                assert option in SELECT_OPTION_MAP[piid]


class TestNumberEntities:
    """Test number entity behavior."""

    def test_countdown_with_value(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.number import CuktechCountdown
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {"9": 30}
        num = CuktechCountdown(coord, mock_entry, 9, "C1 倒计时", "mdi:timer")
        assert num.native_value == 30

    def test_countdown_no_value(self, mock_hass, mock_entry):
        from custom_components.cuktech_charger.number import CuktechCountdown
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord._settings = {}
        num = CuktechCountdown(coord, mock_entry, 9, "C1 倒计时", "mdi:timer")
        assert num.native_value is None


class TestEntityLifecycle:
    """Test entity lifecycle management."""

    def test_notify_callbacks_exception_handling(self, mock_hass, mock_entry):
        """Test _notify_callbacks handles exceptions from callbacks gracefully."""
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        good_cb = MagicMock()
        bad_cb = MagicMock(side_effect=Exception("test error"))
        coord.register_callback(good_cb)
        coord.register_callback(bad_cb)
        coord._notify_callbacks()
        good_cb.assert_called_once()
        bad_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_setting_switch_turn_on_off(self, mock_hass, mock_entry):
        """Test CuktechSettingSwitch async_turn_on/off calls coordinator."""
        from custom_components.cuktech_charger.switch import CuktechSettingSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord.async_set_value = AsyncMock()
        switch = CuktechSettingSwitch(coord, mock_entry, 15, "USB-A", "mdi:usb")
        switch.hass = MagicMock()
        await switch.async_turn_on()
        coord.async_set_value.assert_called_once_with(15, 1)
        await switch.async_turn_off()
        coord.async_set_value.assert_called_with(15, 0)

    @pytest.mark.asyncio
    async def test_port_switch_turn_on_off(self, mock_hass, mock_entry):
        """Test CuktechPortSwitch async_turn_on/off calls coordinator."""
        from custom_components.cuktech_charger.switch import CuktechPortSwitch
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord.async_port_control = AsyncMock()
        switch = CuktechPortSwitch(coord, mock_entry, "c1", "C1", "mdi:usb", 0)
        switch.hass = MagicMock()
        await switch.async_turn_on()
        coord.async_port_control.assert_called_once_with("c1", "on")
        await switch.async_turn_off()
        coord.async_port_control.assert_called_with("c1", "off")

    @pytest.mark.asyncio
    async def test_select_async_select_option(self, mock_hass, mock_entry):
        """Test CuktechSelect async_select_option calls coordinator."""
        from custom_components.cuktech_charger.select import CuktechSelect
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord.async_set_value = AsyncMock()
        sel = CuktechSelect(coord, mock_entry, 5, "scene", "mdi:cog", ["AI模式", "数码生态"])
        sel.hass = MagicMock()
        await sel.async_select_option("AI模式")
        coord.async_set_value.assert_called_once_with(5, 1)

    @pytest.mark.asyncio
    async def test_countdown_set_native_value(self, mock_hass, mock_entry):
        """Test CuktechCountdown async_set_native_value calls coordinator."""
        from custom_components.cuktech_charger.number import CuktechCountdown
        coord = CuktechMQTTCoordinator(mock_hass, mock_entry)
        coord.async_set_value = AsyncMock()
        num = CuktechCountdown(coord, mock_entry, 9, "C1 倒计时", "mdi:timer")
        num.hass = MagicMock()
        await num.async_set_native_value(30.0)
        coord.async_set_value.assert_called_once_with(9, 30)
