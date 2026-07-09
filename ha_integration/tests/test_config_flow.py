"""Tests for HA Integration ConfigFlow."""
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# conftest.py handles all homeassistant mocking
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger.const import DOMAIN


class TestConfigFlow:
    """Test ConfigFlow behavior."""

    def test_validate_input_success(self):
        """Test successful validation with reachable server."""
        from unittest.mock import AsyncMock, MagicMock

        hass = MagicMock()
        session = AsyncMock()
        resp = AsyncMock()
        resp.status = 200
        session.get = AsyncMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=resp), __aexit__=AsyncMock()))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            result = validate_input.__wrapped__(hass, {"name": "Test", "server_url": "http://localhost:8199"})
            # validate_input is not async, so we test it directly
            assert result == {"title": "Test"}

    def test_domain_constant(self):
        """Test DOMAIN constant is correct."""
        assert DOMAIN == "cuktech_charger"

    def test_default_server_url(self):
        """Test default server URL."""
        from custom_components.cuktech_charger.const import DEFAULT_SERVER_URL
        assert DEFAULT_SERVER_URL == "http://localhost:8199"
