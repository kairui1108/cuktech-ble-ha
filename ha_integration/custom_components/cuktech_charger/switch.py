"""Switch platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SETTING_PIIDS = {
    15: {"name": "USB-A常通电", "icon": "mdi:usb-port"},
    19: {"name": "空闲息屏", "icon": "mdi:monitor-off"},
    20: {"name": "屏幕方向锁", "icon": "mdi:screen-rotation-lock"},
}

PORT_SWITCHES = {
    "c1": {"name": "C1 端口", "icon": "mdi:usb-c-port", "bit": 0},
    "c2": {"name": "C2 端口", "icon": "mdi:usb-c-port", "bit": 1},
    "c3": {"name": "C3 端口", "icon": "mdi:usb-c-port", "bit": 2},
    "a": {"name": "USB-A 端口", "icon": "mdi:usb-port", "bit": 3},
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger switches from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for piid, cfg in SETTING_PIIDS.items():
        entities.append(CuktechSettingSwitch(coord, entry, piid, cfg["name"], cfg["icon"]))

    for port, cfg in PORT_SWITCHES.items():
        entities.append(CuktechPortSwitch(coord, entry, port, cfg["name"], cfg["icon"], cfg["bit"]))

    async_add_entities(entities)


class CuktechSettingSwitch(SwitchEntity):
    """Switch for CUKTECH Charger settings."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coord
        self._entry = entry
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_switch_{piid}"
        self._attr_name = name
        self._attr_icon = icon
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
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}, **self.coordinator.device_info}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if not self.coordinator.data:
            return None
        v = self.coordinator.data.get(str(self._piid))
        return bool(v) if v is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_set_value(self._piid, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_set_value(self._piid, 0)


class CuktechPortSwitch(SwitchEntity):
    """Switch for CUKTECH Charger ports."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        port: str,
        name: str,
        icon: str,
        bit: int,
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coord
        self._entry = entry
        self._port = port
        self._bit = bit
        self._attr_unique_id = f"{entry.entry_id}_port_switch_{port}"
        self._attr_name = name
        self._attr_icon = icon
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
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}, **self.coordinator.device_info}

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if not self.coordinator.data:
            return None
        port_ctl = self.coordinator.data.get("16")
        if port_ctl is None:
            return None
        return bool(port_ctl & (1 << self._bit))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_port_control(self._port, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_port_control(self._port, "off")
