"""Binary sensor platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_ALL, CB_TYPE_PORT
from .const import DOMAIN, PORT_NAMES


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger binary sensors from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CuktechPortActive(coord, entry, piid, name)
        for piid, name in PORT_NAMES.items()
    ]
    entities.append(CuktechConnectionBinarySensor(coord, entry))
    async_add_entities(entities)


class CuktechPortActive(CuktechBaseEntity, BinarySensorEntity):
    """Binary sensor for CUKTECH Charger port active status."""

    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        port_name: str,
    ) -> None:
        """Initialize the binary sensor."""
        self._piid = piid
        self._port_name = port_name
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_active"
        self._attr_name = f"{port_name} Active"
        super().__init__(coord, entry, CB_TYPE_PORT)

    @property
    def is_on(self) -> bool | None:
        """Return True if port is active."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        return pd.get("active", False)


class CuktechConnectionBinarySensor(CuktechBaseEntity, BinarySensorEntity):
    """Binary sensor showing BLE device connection status."""

    _attr_name = "连接状态"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord: CuktechMQTTCoordinator, entry: ConfigEntry) -> None:
        """Initialize the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_ble_connected"
        super().__init__(coord, entry, CB_TYPE_ALL)

    @property
    def is_on(self) -> bool | None:
        """Return True if device is connected."""
        return self.coordinator.ble_connected
