"""Shared fixtures for CUKTECH HA Integration tests."""
import sys
import types
from unittest.mock import MagicMock

# Create a real module for homeassistant.core with @callback as passthrough
ha_core = types.ModuleType("homeassistant.core")
ha_core.callback = lambda func: func
ha_core.HomeAssistant = MagicMock

# Mock other homeassistant modules
sys.modules['homeassistant'] = MagicMock()
sys.modules['homeassistant.components'] = MagicMock()
sys.modules['homeassistant.components.mqtt'] = MagicMock()
sys.modules['homeassistant.components.select'] = MagicMock()
sys.modules['homeassistant.config_entries'] = MagicMock()
sys.modules['homeassistant.const'] = MagicMock()
sys.modules['homeassistant.core'] = ha_core
sys.modules['homeassistant.exceptions'] = MagicMock()
sys.modules['homeassistant.helpers'] = MagicMock()
sys.modules['homeassistant.helpers.aiohttp_client'] = MagicMock()
sys.modules['homeassistant.helpers.entity_platform'] = MagicMock()
sys.modules['homeassistant.helpers.event'] = MagicMock()

import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.data = {}
    hass.async_add_executor_job = AsyncMock()
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
