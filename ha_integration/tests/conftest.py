"""Shared fixtures for CUKTECH HA Integration tests."""
import sys
import types
from unittest.mock import MagicMock, AsyncMock


class _FakeEntity:
    def __init__(self, *args, **kwargs):
        pass


class _FakeSensorEntity(_FakeEntity):
    pass


class _FakeBinarySensorEntity(_FakeEntity):
    pass


class _FakeSwitchEntity(_FakeEntity):
    pass


class _FakeSelectEntity(_FakeEntity):
    pass


class _FakeNumberEntity(_FakeEntity):
    pass


ha_core = types.ModuleType("homeassistant.core")
ha_core.callback = lambda func: func
ha_core.HomeAssistant = MagicMock

ha_helpers = types.ModuleType("homeassistant.helpers")
ha_helpers.aiohttp_client = MagicMock()
ha_helpers.entity_platform = MagicMock()
ha_helpers.event = MagicMock()

ha_components = types.ModuleType("homeassistant.components")
ha_components.mqtt = types.ModuleType("homeassistant.components.mqtt")
ha_components.mqtt.async_publish = AsyncMock()
ha_components.mqtt.async_subscribe = AsyncMock()
ha_components.sensor = types.ModuleType("homeassistant.components.sensor")
ha_components.sensor.SensorEntity = _FakeSensorEntity
ha_components.sensor.SensorEntityDescription = MagicMock
ha_components.sensor.SensorDeviceClass = MagicMock()
ha_components.sensor.SensorStateClass = MagicMock()
ha_components.sensor.UnitOfPower = MagicMock()
ha_components.binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
ha_components.binary_sensor.BinarySensorEntity = _FakeBinarySensorEntity
ha_components.binary_sensor.BinarySensorDeviceClass = MagicMock()
ha_components.switch = types.ModuleType("homeassistant.components.switch")
ha_components.switch.SwitchEntity = _FakeSwitchEntity
ha_components.switch.SwitchDeviceClass = MagicMock()
ha_components.select = types.ModuleType("homeassistant.components.select")
ha_components.select.SelectEntity = _FakeSelectEntity
ha_components.number = types.ModuleType("homeassistant.components.number")
ha_components.number.NumberEntity = _FakeNumberEntity
ha_components.number.NumberMode = MagicMock()

ha_exceptions = types.ModuleType("homeassistant.exceptions")
ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

ha_data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
ha_data_entry_flow.FlowResult = dict
ha_data_entry_flow.AbortFlow = type("AbortFlow", (Exception,), {})
sys.modules['homeassistant.data_entry_flow'] = ha_data_entry_flow

class _FakeConfigFlow:
    VERSION = 1
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

ha_config = types.ModuleType("homeassistant.config_entries")
ha_config.ConfigEntry = MagicMock()
ha_config.ConfigFlow = _FakeConfigFlow

ha_const = types.ModuleType("homeassistant.const")
ha_const.Platform = MagicMock()
ha_const.EntityCategory = MagicMock()
ha_const.UnitOfElectricCurrent = MagicMock()
ha_const.UnitOfElectricPotential = MagicMock()
ha_const.UnitOfPower = MagicMock()
ha_const.CONF_NAME = "name"

_vol = types.ModuleType("voluptuous")
_vol.Schema = lambda s: s
_vol.Optional = lambda key, **kwargs: key
sys.modules['voluptuous'] = _vol

ha = types.ModuleType("homeassistant")
ha.HomeAssistant = MagicMock

sys.modules['homeassistant'] = ha
sys.modules['homeassistant.core'] = ha_core
sys.modules['homeassistant.const'] = ha_const
sys.modules['homeassistant.config_entries'] = ha_config
sys.modules['homeassistant.exceptions'] = ha_exceptions
sys.modules['homeassistant.helpers'] = ha_helpers
sys.modules['homeassistant.helpers.aiohttp_client'] = ha_helpers.aiohttp_client
sys.modules['homeassistant.helpers.entity_platform'] = ha_helpers.entity_platform
sys.modules['homeassistant.helpers.event'] = ha_helpers.event
sys.modules['homeassistant.components'] = ha_components
sys.modules['homeassistant.components.mqtt'] = ha_components.mqtt
sys.modules['homeassistant.components.sensor'] = ha_components.sensor
sys.modules['homeassistant.components.binary_sensor'] = ha_components.binary_sensor
sys.modules['homeassistant.components.switch'] = ha_components.switch
sys.modules['homeassistant.components.select'] = ha_components.select
sys.modules['homeassistant.components.number'] = ha_components.number


import pytest


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.data = {}
    hass.async_add_executor_job = MagicMock()
    return hass


@pytest.fixture
def mock_entry():
    """Create a mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_123"
    entry.data = {
        "name": "CUKTECH Charger",
        "server_url": "http://localhost:8199",
    }
    return entry
