"""Tests for HA Integration ConfigFlow."""
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from custom_components.cuktech_charger.const import DOMAIN, DEFAULT_SERVER_URL


class TestConfigFlowValidation:
    """Test validate_input function."""

    @pytest.mark.asyncio
    async def test_validate_input_success(self):
        """Test successful validation with reachable server."""
        hass = MagicMock()
        session = MagicMock()
        session.get = AsyncMock(return_value=SimpleNamespace(status=200))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            result = await validate_input(hass, {"name": "Test", "server_url": "http://localhost:8199"})
            assert result == {"title": "Test"}

    @pytest.mark.asyncio
    async def test_validate_input_connection_error(self):
        """Test validation fails when server is unreachable."""
        hass = MagicMock()
        session = MagicMock()
        session.get = AsyncMock(side_effect=Exception("Connection refused"))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            with pytest.raises(ValueError, match="Cannot connect"):
                await validate_input(hass, {"name": "Test", "server_url": "http://unreachable:9999"})

    @pytest.mark.asyncio
    async def test_validate_input_bad_status(self):
        """Test validation fails with non-200 status."""
        hass = MagicMock()
        session = MagicMock()
        session.get = AsyncMock(return_value=SimpleNamespace(status=500))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            from custom_components.cuktech_charger.config_flow import validate_input
            with pytest.raises(ValueError, match="status 500"):
                await validate_input(hass, {"name": "Test", "server_url": "http://localhost:8199"})


class TestConfigFlowStep:
    """Test ConfigFlow.async_step_user flow logic."""

    def _make_flow(self, hass):
        """Create a ConfigFlow instance with mocked base class."""
        from custom_components.cuktech_charger.config_flow import ConfigFlow
        flow = ConfigFlow.__new__(ConfigFlow)
        flow.hass = hass
        flow.context = {}
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry", "title": "Test", "data": {}})
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "user"})
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        return flow

    @pytest.mark.asyncio
    async def test_step_user_shows_form(self):
        """Test step_user shows form when no input."""
        hass = MagicMock()
        flow = self._make_flow(hass)
        result = await flow.async_step_user(None)
        flow.async_show_form.assert_called_once()
        assert result["type"] == "form"

    @pytest.mark.asyncio
    async def test_step_user_success(self):
        """Test step_user creates entry on success."""
        hass = MagicMock()
        flow = self._make_flow(hass)
        session = MagicMock()
        session.get = AsyncMock(return_value=SimpleNamespace(status=200))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            result = await flow.async_step_user({"name": "Test", "server_url": "http://localhost:8199"})
        flow.async_create_entry.assert_called_once()
        assert result["type"] == "create_entry"
        call_kwargs = flow.async_create_entry.call_args[1]
        assert call_kwargs["title"] == "Test"
        assert call_kwargs["data"]["name"] == "Test"
        assert call_kwargs["data"]["server_url"] == "http://localhost:8199"

    @pytest.mark.asyncio
    async def test_step_user_unique_id(self):
        """Test step_user sets unique_id from server_url."""
        hass = MagicMock()
        flow = self._make_flow(hass)
        session = MagicMock()
        session.get = AsyncMock(return_value=SimpleNamespace(status=200))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            await flow.async_step_user({"name": "Test", "server_url": "http://localhost:8199"})
        flow.async_set_unique_id.assert_called_once()
        flow._abort_if_unique_id_configured.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_user_validation_error(self):
        """Test step_user shows error on validation failure."""
        hass = MagicMock()
        flow = self._make_flow(hass)
        session = MagicMock()
        session.get = AsyncMock(side_effect=Exception("Connection refused"))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            result = await flow.async_step_user({"name": "Test", "server_url": "http://unreachable:9999"})
        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args
        assert call_kwargs[1]["errors"]["base"]

    @pytest.mark.asyncio
    async def test_step_user_duplicate_unique_id(self):
        """Test step_user aborts if unique_id already configured."""
        hass = MagicMock()
        flow = self._make_flow(hass)
        session = MagicMock()
        session.get = AsyncMock(return_value=SimpleNamespace(status=200))

        from homeassistant.data_entry_flow import AbortFlow
        flow._abort_if_unique_id_configured = MagicMock(side_effect=AbortFlow("already_configured"))

        with patch('custom_components.cuktech_charger.config_flow.async_get_clientsession', return_value=session):
            with pytest.raises(AbortFlow, match="already_configured"):
                await flow.async_step_user({"name": "Test", "server_url": "http://localhost:8199"})


class TestDomainConstant:
    """Test constants."""

    def test_domain(self):
        assert DOMAIN == "cuktech_charger"

    def test_default_server_url(self):
        assert DEFAULT_SERVER_URL == "http://localhost:8199"
