"""Tests for HA Integration ConfigFlow."""
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# conftest.py handles all homeassistant mocking
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger.const import DOMAIN, DEFAULT_SERVER_URL


class TestConfigFlow:
    """Test ConfigFlow behavior."""

    @pytest.mark.asyncio
    async def test_validate_input_success(self):
        """Test successful validation with reachable server."""
        hass = MagicMock()
        resp = AsyncMock()
        resp.status = 200
        session = AsyncMock()
        session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock()))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            result = await validate_input(hass, {"name": "Test", "server_url": "http://localhost:8199"})
            assert result == {"title": "Test"}

    @pytest.mark.asyncio
    async def test_validate_input_connection_error(self):
        """Test validation fails when server is unreachable."""
        hass = MagicMock()
        session = AsyncMock()
        session.get = AsyncMock(side_effect=Exception("Connection refused"))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            with pytest.raises(ValueError, match="Cannot connect"):
                await validate_input(hass, {"name": "Test", "server_url": "http://unreachable:9999"})

    @pytest.mark.asyncio
    async def test_validate_input_bad_status(self):
        """Test validation fails with non-200 status."""
        hass = MagicMock()
        resp = AsyncMock()
        resp.status = 500
        session = AsyncMock()
        session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock()))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            with pytest.raises(ValueError, match="status 500"):
                await validate_input(hass, {"name": "Test", "server_url": "http://localhost:8199"})

    def test_domain_constant(self):
        """Test DOMAIN constant is correct."""
        assert DOMAIN == "cuktech_charger"

    def test_default_server_url(self):
        """Test default server URL."""
        assert DEFAULT_SERVER_URL == "http://localhost:8199"
