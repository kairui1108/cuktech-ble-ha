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

# 各端口支持的协议开关定义
PROTOCOL_SWITCHES = [
    ("c1", "pd", "C1 PD"),
    ("c1", "pps", "C1 PPS"),
    ("c1", "ufcs", "C1 UFCS"),
    ("c2", "pd", "C2 PD"),
    ("c2", "pps", "C2 PPS"),
    ("c2", "ufcs", "C2 UFCS"),
    ("c3", "ufcs", "C3 UFCS"),
    ("c3", "scp", "C3 SCP"),
    ("a", "ufcs", "USB-A UFCS"),
    ("a", "scp", "USB-A SCP"),
]

SETTING_PIIDS = {
    15: {"name": "USB-A小电流", "icon": "mdi:usb-port"},
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

    for port, proto, name in PROTOCOL_SWITCHES:
        entities.append(CuktechProtocolSwitch(coord, entry, port, proto, name))

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
    """Switch for individual protocol on a CUKTECH Charger port (PIID 21)."""

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        port: str,
        protocol: str,
        name: str,
    ) -> None:
        """Initialize the protocol switch."""
        self.coordinator = coord
        self._entry = entry
        self._port = port
        self._protocol = protocol
        self._attr_unique_id = f"{entry.entry_id}_protocol_{port}_{protocol}"
        self._attr_name = name
        self._attr_icon = "mdi:power-plug-outline"
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
        """Return True if the protocol switch is on.

        对于 C1/C2 的 PPS，同时检查 PD 状态：PD 关闭时 PPS 视为关闭。
        """
        switches = self.coordinator.protocol_switches
        port_data = switches.get(self._port)
        if port_data is None:
            return None
        if self._protocol == 'pps' and port_data.get('pd') is False:
            return False
        return port_data.get(self._protocol)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the protocol switch on."""
        await self.coordinator.async_set_protocol(self._port, self._protocol, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the protocol switch off."""
        await self.coordinator.async_set_protocol(self._port, self._protocol, False)
