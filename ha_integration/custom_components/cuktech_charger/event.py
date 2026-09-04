"""Event platform for CUKTECH Charger — charge completion events."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_CHARGE
from .const import DOMAIN

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


class CuktechChargeEvent(CuktechBaseEntity, EventEntity):
    """Event entity for charge completion notifications."""

    entity_description = EVENT_DESCRIPTION

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the event entity."""
        self._attr_unique_id = f"{entry.entry_id}_charge_event"
        super().__init__(coord, entry, CB_TYPE_CHARGE)

    @callback
    def _update(self) -> None:
        """Handle charge event (override base: trigger event, not state write)."""
        if self.hass is None:
            return
        event = self.coordinator.last_charge_event
        if event is None:
            return
        self._trigger_event("charge_end", event)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return event data as entity attributes."""
        return self.coordinator.last_charge_event or {}
