"""Select platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .const import DOMAIN, DEVICE_INFO, PIID_DISPLAY, SELECT_PIIDS, SELECT_OPTION_MAP

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger selects from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechSelect(coord, entry, piid, cfg["name"], cfg["icon"], cfg["options"])
        for piid, cfg in SELECT_PIIDS.items()
    ]
    async_add_entities(entities)


class CuktechSelect(SelectEntity):
    """Select entity for CUKTECH Charger settings."""

    _attr_has_entity_name = True

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
        self.coordinator = coord
        self._entry = entry
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_select_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options
        coord.register_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback when removed."""
        self.coordinator.unregister_callback(self._update)
        await super().async_will_remove_from_hass()

    @callback
    def _update(self) -> None:
        """Handle state update."""
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}, **DEVICE_INFO}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        if not self.coordinator._settings:
            return None
        v = self.coordinator._settings.get(str(self._piid))
        if v is None:
            return None
        return PIID_DISPLAY.get(self._piid, {}).get(v)

    async def async_select_option(self, option: str) -> None:
        """Select an option."""
        option_map = SELECT_OPTION_MAP.get(self._piid, {})
        value = option_map.get(option)
        if value is not None:
            await self.coordinator.async_set_value(self._piid, value)
