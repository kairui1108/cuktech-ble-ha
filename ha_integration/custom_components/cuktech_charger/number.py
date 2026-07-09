"""Number platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .const import DOMAIN, DEVICE_INFO

_LOGGER = logging.getLogger(__name__)

COUNTDOWN_PIIDS = {
    9: {"name": "C1 倒计时", "icon": "mdi:timer-cog-outline"},
    10: {"name": "C2 倒计时", "icon": "mdi:timer-cog-outline"},
    11: {"name": "C3 倒计时", "icon": "mdi:timer-cog-outline"},
    12: {"name": "USB-A 倒计时", "icon": "mdi:timer-cog-outline"},
}


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


class CuktechCountdown(NumberEntity):
    """Number entity for CUKTECH Charger countdown timers."""

    _attr_has_entity_name = True
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
        self.coordinator = coord
        self._entry = entry
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_countdown_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        coord.register_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback when removed."""
        self.coordinator.unregister_callback(self._update)

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
