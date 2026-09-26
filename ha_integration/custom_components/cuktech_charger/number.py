"""Number platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.const import UnitOfEnergy

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_SETTINGS
from .const import (
    DOMAIN,
    COUNTDOWN_PIIDS,
    CHARGE_LIMIT_PORTS,
    CHARGE_LIMIT_ICON,
    MAX_LIMIT_WH,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger countdown timers from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechCountdown(coord, entry, piid, cfg["name"], cfg["icon"])
        for piid, cfg in COUNTDOWN_PIIDS.items()
    ]
    entities.extend(
        CuktechChargeLimit(coord, entry, port, label)
        for port, label in CHARGE_LIMIT_PORTS.items()
    )
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


class CuktechChargeLimit(CuktechBaseEntity, NumberEntity):
    """Number entity for per-port charge limit (Wh) — auto power-off threshold.

    Value semantics: 0 = disabled (no limit). Units are *charger output energy*
    accumulated by V×I integration, not the energy actually stored in the
    device being charged (cable + conversion losses make the latter 5~15%
    smaller). This mirrors ble_server's /api/charge-limits semantics exactly.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = MAX_LIMIT_WH
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_mode = NumberMode.BOX
    _attr_icon = CHARGE_LIMIT_ICON

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        port: str,
        label: str,
    ) -> None:
        """Initialize the charge limit entity."""
        self._port = port
        self._attr_unique_id = f"{entry.entry_id}_charge_limit_{port}"
        self._attr_name = f"{label} charge limit"
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def native_value(self) -> float | None:
        """Return the configured limit Wh (0 = disabled)."""
        return self.coordinator.charge_limit_wh(self._port)

    async def async_set_native_value(self, value: float) -> None:
        """Set the charge limit; 0 disables."""
        await self.coordinator.async_set_charge_limit(self._port, value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the effective mode so automations can reason about re-arming."""
        return {
            "port": self._port,
            "mode": self.coordinator.charge_limit_mode(self._port),
            "backend": self.coordinator.limit_backend,
        }
