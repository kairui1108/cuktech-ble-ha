"""Event platform for CUKTECH Charger — charge completion events."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

EVENT_DESCRIPTION = EventEntityDescription(
    key="charge_end",
    name="Charge Complete",
    icon="mdi:battery-check",
    event_types=["charge_end"],
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger event entities."""
    coord = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CuktechChargeEvent(coord, entry)])


class CuktechChargeEvent(EventEntity):
    """Event entity for charge completion notifications."""

    _attr_has_entity_name = True
    entity_description = EVENT_DESCRIPTION

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the event entity."""
        self.coordinator = coord
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_event"

    async def async_added_to_hass(self) -> None:
        """Register callback when added to hass."""
        await super().async_added_to_hass()
        self.coordinator.register_charge_event_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback when removed."""
        self.coordinator.unregister_charge_event_callback(self._update)
        await super().async_will_remove_from_hass()

    @callback
    def _update(self) -> None:
        """Handle charge event."""
        if self.hass is None:
            return
        event = self.coordinator.last_charge_event
        if event is None:
            return
        self._trigger_event("charge_end", event)
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}, **self.coordinator.device_info}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return event data as entity attributes."""
        return self.coordinator.last_charge_event or {}
