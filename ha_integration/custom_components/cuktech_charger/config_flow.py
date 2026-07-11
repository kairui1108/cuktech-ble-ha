"""Config flow for CUKTECH Charger integration."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_SERVER_URL, DEFAULT_SERVER_URL

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default="CUKTECH 10 GaN Charger Ultra"): str,
        vol.Optional(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Validate the user input allows us to connect."""
    server_url = data.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
    session = async_get_clientsession(hass)
    try:
        url = f"{server_url}/api/status"
        resp = await session.get(url, timeout=10)
        if resp.status != 200:
            raise ValueError(f"Server returned status {resp.status}")
    except ValueError:
        raise
    except Exception as err:
        raise ValueError(f"Cannot connect to server: {err}") from err
    return {"title": data.get(CONF_NAME, "CUKTECH Charger")}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CUKTECH Charger."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except ValueError as err:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                server_url = user_input.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
                conf_name = user_input.get(CONF_NAME, "CUKTECH Charger")
                unique_id = hashlib.md5(server_url.encode()).hexdigest()[:16]
                await self.async_set_unique_id(f"cuktech_{unique_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=conf_name, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle re-authentication when the server URL changes."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except ValueError as err:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                if entry:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **user_input}
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_SERVER_URL): str}
            ),
            errors=errors,
        )
