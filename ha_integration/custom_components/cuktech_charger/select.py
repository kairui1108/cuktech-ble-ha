"""Select platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_SETTINGS
from .const import (
    DOMAIN,
    PIID_DISPLAY,
    SELECT_PIIDS,
    SELECT_OPTION_MAP,
    CHARGE_LIMIT_PORTS,
    CHARGE_LIMIT_ICON,
    LIMIT_MODES,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger selects from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechSelect(coord, entry, piid, cfg["name"], cfg["icon"], cfg["options"])
        for piid, cfg in SELECT_PIIDS.items()
    ]
    entities.extend(
        CuktechChargeLimitMode(coord, entry, port, label)
        for port, label in CHARGE_LIMIT_PORTS.items()
    )
    async_add_entities(entities)


class CuktechSelect(CuktechBaseEntity, SelectEntity):
    """Select entity for CUKTECH Charger settings."""

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        name: str,
        icon: str,
        options: list[str],
    ) -> None:
        """Initialize the select entity."""
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_select_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        if not self.coordinator.data:
            return None
        v = self.coordinator.data.get(str(self._piid))
        if v is None:
            return None
        display = PIID_DISPLAY.get(self._piid, {}).get(v)
        if display is not None:
            return display
        return None

    async def async_select_option(self, option: str) -> None:
        """Select an option."""
        option_map = SELECT_OPTION_MAP.get(self._piid, {})
        value = option_map.get(option)
        if value is not None:
            await self.coordinator.async_set_value(self._piid, value)


class CuktechChargeLimitMode(CuktechBaseEntity, SelectEntity):
    """Per-port charge-limit mode: once (consume after firing) / always (re-arm).

    Options are the raw modes used by ble_server's REST API (`once`/`always`),
    not display translations, so templates and automations can pass them
    through unchanged.
    """

    _attr_options = list(LIMIT_MODES)
    _attr_icon = CHARGE_LIMIT_ICON

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        port: str,
        label: str,
    ) -> None:
        """Initialize the mode select."""
        self._port = port
        self._attr_unique_id = f"{entry.entry_id}_charge_limit_mode_{port}"
        self._attr_name = f"{label} charge limit mode"
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def current_option(self) -> str | None:
        """Return the active mode."""
        mode = self.coordinator.charge_limit_mode(self._port)
        return mode if mode in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Select the mode, keeping the configured Wh untouched."""
        if option not in self._attr_options:
            return
        await self.coordinator.async_set_charge_limit(
            self._port, self.coordinator.charge_limit_wh(self._port), option
        )
