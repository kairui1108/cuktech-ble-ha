"""Switch platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_ALL, CB_TYPE_SETTINGS
from .const import DOMAIN, PROTOCOL_SWITCHES, SETTING_PIIDS, PORT_SWITCHES

_LOGGER = logging.getLogger(__name__)


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


class CuktechConnectionSwitch(CuktechBaseEntity, SwitchEntity):
    """Switch to control BLE connection (enable/disable)."""

    _attr_name = "连接控制"
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coord: CuktechMQTTCoordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        self._attr_unique_id = f"{entry.entry_id}_ble_control"
        super().__init__(coord, entry, CB_TYPE_ALL)

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


class CuktechSettingSwitch(CuktechBaseEntity, SwitchEntity):
    """Switch for CUKTECH Charger settings."""

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
        self._piid = piid
        self._attr_unique_id = f"{entry.entry_id}_switch_{piid}"
        self._attr_name = name
        self._attr_icon = icon
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if not self.coordinator.data:
            return None
        v = self.coordinator.data.get(str(self._piid))
        if v is None:
            return None
        # 兼容数值与字符串 ("0"/"1"): bool("0") 为 True 是陷阱，必须显式转 int
        try:
            return int(v) != 0
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid setting value for piid=%s: %r", self._piid, v)
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_set_value(self._piid, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_set_value(self._piid, 0)


class CuktechPortSwitch(CuktechBaseEntity, SwitchEntity):
    """Switch for CUKTECH Charger ports."""

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
        self._port = port
        self._bit = bit
        self._attr_unique_id = f"{entry.entry_id}_port_switch_{port}"
        self._attr_name = name
        self._attr_icon = icon
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if not self.coordinator.data:
            return None
        port_ctl = self.coordinator.data.get("16")
        if port_ctl is None:
            return None
        # 兼容数值与字符串 (如 "15")：统一转 int，避免字符串与位运算抛 TypeError
        try:
            port_ctl = int(port_ctl)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid port control value: %r", port_ctl)
            return None
        return bool(port_ctl & (1 << self._bit))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        await self.coordinator.async_port_control(self._port, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        await self.coordinator.async_port_control(self._port, "off")


class CuktechProtocolSwitch(CuktechBaseEntity, SwitchEntity):
    """Switch for individual protocol on a CUKTECH Charger port (PIID 21)."""

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
        self._port = port
        self._protocol = protocol
        self._attr_unique_id = f"{entry.entry_id}_protocol_{port}_{protocol}"
        self._attr_name = name
        self._attr_icon = "mdi:power-plug-outline"
        super().__init__(coord, entry, CB_TYPE_SETTINGS)

    @property
    def is_on(self) -> bool | None:
        """Return True if the protocol switch is on.

        对于 C1/C2 的 PPS，同时检查 PD 状态：PD 关闭时 PPS 视为关闭。
        """
        switches = self.coordinator.protocol_switches
        port_data = switches.get(self._port)
        if port_data is None:
            return None
        if self._protocol == "pps" and port_data.get("pd") is False:
            return False
        return port_data.get(self._protocol)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the protocol switch on."""
        await self.coordinator.async_set_protocol(self._port, self._protocol, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the protocol switch off."""
        await self.coordinator.async_set_protocol(self._port, self._protocol, False)
