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
from .const import TOPIC_PROTOCOL as _  # noqa: F401

_LOGGER = logging.getLogger(__name__)

# 协议开关定义 (对齐米家 PIID 21 protocol_ctl_extend)
# bit 定义: c1_flags={pd:0, pps:1, ufcs:2}, c2={pd:8, pps:9, ufcs:10}
#            c3_flags={ufcs:16, scp:17}, a={ufcs:24, scp:25}
PROTOCOL_SWITCHES = {
    "c1_pd":   {"port": "c1", "proto": "pd",   "name": "C1 PD",   "icon": "mdi:flash", "bit": 0},
    "c1_pps":  {"port": "c1", "proto": "pps",  "name": "C1 PPS",  "icon": "mdi:flash", "bit": 1},
    "c1_ufcs": {"port": "c1", "proto": "ufcs", "name": "C1 UFCS", "icon": "mdi:flash", "bit": 2},
    "c2_pd":   {"port": "c2", "proto": "pd",   "name": "C2 PD",   "icon": "mdi:flash", "bit": 8},
    "c2_pps":  {"port": "c2", "proto": "pps",  "name": "C2 PPS",  "icon": "mdi:flash", "bit": 9},
    "c2_ufcs": {"port": "c2", "proto": "ufcs", "name": "C2 UFCS", "icon": "mdi:flash", "bit": 10},
    "c3_ufcs": {"port": "c3", "proto": "ufcs", "name": "C3 UFCS", "icon": "mdi:flash", "bit": 16},
    "c3_scp":  {"port": "c3", "proto": "scp",  "name": "C3 SCP",  "icon": "mdi:flash", "bit": 17},
    "a_ufcs":  {"port": "a",  "proto": "ufcs", "name": "A UFCS",  "icon": "mdi:flash", "bit": 24},
    "a_scp":   {"port": "a",  "proto": "scp",  "name": "A SCP",   "icon": "mdi:flash", "bit": 25},
}

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
    entities = [CuktechConnectionSwitch(coord, entry)]

    for piid, cfg in SETTING_PIIDS.items():
        entities.append(CuktechSettingSwitch(coord, entry, piid, cfg["name"], cfg["icon"]))

    for port, cfg in PORT_SWITCHES.items():
        entities.append(CuktechPortSwitch(coord, entry, port, cfg["name"], cfg["icon"], cfg["bit"]))

    for key, cfg in PROTOCOL_SWITCHES.items():
        entities.append(CuktechProtocolSwitch(
            coord, entry, cfg["port"], cfg["proto"],
            cfg["name"], cfg["icon"], cfg["bit"]
        ))

    async_add_entities(entities)


class CuktechConnectionSwitch(SwitchEntity):
    """Switch to control BLE connection (enable/disable)."""

    _attr_has_entity_name = True
    _attr_name = "连接控制"
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coord: CuktechMQTTCoordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        self.coordinator = coord
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ble_control"
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
        return self.coordinator.available and not self.coordinator.ble_pending

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for frontend display."""
        return {"pending": self.coordinator.ble_pending}

    @property
    def is_on(self) -> bool | None:
        """Return True if BLE connection is enabled."""
        return self.coordinator.ble_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable BLE connection."""
        await self.coordinator.async_enable_ble(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable BLE connection."""
        await self.coordinator.async_enable_ble(False)


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


class CuktechProtocolSwitch(SwitchEntity):
    """Switch for CUKTECH Charger protocol control (PIID 21)."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        port: str,
        proto: str,
        name: str,
        icon: str,
        bit: int,
    ) -> None:
        """Initialize the protocol switch."""
        self.coordinator = coord
        self._entry = entry
        self._port = port
        self._proto = proto
        self._bit = bit
        self._attr_unique_id = f"{entry.entry_id}_proto_{port}_{proto}"
        self._attr_name = name
        self._attr_icon = icon
        coord.register_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback."""
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
        """Return True if available."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return True if the protocol is enabled for this port."""
        if not self.coordinator.data:
            return None
        val = self.coordinator.data.get("21")
        if val is None:
            return None
        return bool(val & (1 << self._bit))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Toggle protocol on."""
        if not self.is_on:
            await self.coordinator.async_protocol_toggle(self._port, self._proto)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Toggle protocol off."""
        if self.is_on:
            await self.coordinator.async_protocol_toggle(self._port, self._proto)
