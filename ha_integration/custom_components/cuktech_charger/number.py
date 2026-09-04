"""Number platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_SETTINGS
from .const import DOMAIN, COUNTDOWN_PIIDS


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger countdown timers from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechCountdown(coord, entry, piid, cfg["name"], cfg["icon"])
        for piid, cfg in COUNTDOWN_PIIDS.items()
    ]
    async_add_entities(entities)


class CuktechCountdown(CuktechBaseEntity, NumberEntity):
    """Number entity for CUKTECH Charger countdown timers."""

    _attr_native_min_value = 0
    _attr_native_max_value = 1440
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the number entity."""
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_countdown_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def native_value(self) -> float | None:
        """Return the countdown value."""
        if not self.coordinator.data:
            return None
        v = self.coordinator.data.get(str(self._piid))
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the countdown value."""
        await self.coordinator.async_set_value(self._piid, int(value))
