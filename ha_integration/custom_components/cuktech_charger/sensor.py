"""Sensor platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .base_entity import CuktechBaseEntity, CB_TYPE_PORT
from .const import DOMAIN, PORT_MAP, PORT_NAMES, PROTOCOL_OPTIONS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger sensors from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities: list = []

    for piid, pname in PORT_NAMES.items():
        for st in ("voltage", "current", "power"):
            entities.append(CuktechPortSensor(coord, entry, piid, pname, st))
        entities.append(CuktechPortProtocolSensor(coord, entry, piid, pname))

    entities.append(CuktechTotalPowerSensor(coord, entry))
    async_add_entities(entities)


class CuktechPortSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for CUKTECH Charger port data."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    UNITS = {
        "voltage": UnitOfElectricPotential.VOLT,
        "current": UnitOfElectricCurrent.AMPERE,
        "power": UnitOfPower.WATT,
    }

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        port_name: str,
        sensor_type: str,
    ) -> None:
        """Initialize the sensor."""
        self._piid = piid
        self._port_name = port_name
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_{sensor_type}"
        self._attr_name = f"{port_name} {sensor_type}"
        self._attr_native_unit_of_measurement = self.UNITS.get(sensor_type)
        super().__init__(coord, entry, CB_TYPE_PORT)

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        value = pd.get(self._sensor_type)
        if value is None:
            return None
        # ble_server 可能下发字符串 (如 "20.5")；统一转 float，避免 HA 数值转换告警
        try:
            return float(value)
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid numeric %s for port %s: %r",
                            self._sensor_type, self._port_name, value)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return {}
        return {"port": self._port_name, "active": pd.get("active", False)}


class CuktechTotalPowerSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for total power consumption."""

    _attr_name = "Total Power"
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: CuktechMQTTCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = f"{entry.entry_id}_total_power"
        super().__init__(coord, entry, CB_TYPE_PORT)

    @property
    def native_value(self) -> float:
        """Return the total power."""
        total = 0.0
        for piid in PORT_MAP.values():
            pd = self.coordinator.port_data.get(str(piid))
            if pd and pd.get("active"):
                # power 可能为 None 或字符串，统一转 float，避免 total += None 抛 TypeError
                try:
                    total += float(pd.get("power") or 0)
                except (TypeError, ValueError):
                    _LOGGER.warning("Invalid power value for port %s: %r", piid, pd.get("power"))
        return round(total, 1)


class CuktechPortProtocolSensor(CuktechBaseEntity, SensorEntity):
    """Sensor for CUKTECH Charger port protocol."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PROTOCOL_OPTIONS

    def __init__(
        self,
        coord: CuktechMQTTCoordinator,
        entry: ConfigEntry,
        piid: int,
        port_name: str,
    ) -> None:
        """Initialize the sensor."""
        self._piid = piid
        self._port_name = port_name
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_protocol"
        self._attr_name = f"{port_name} Protocol"
        self._attr_icon = "mdi:usb-c-port"
        super().__init__(coord, entry, CB_TYPE_PORT)

    @property
    def native_value(self) -> str | None:
        """Return the current protocol."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        protocol = pd.get("protocol", "idle")
        if protocol in PROTOCOL_OPTIONS:
            return protocol
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return {}
        return {"port": self._port_name, "active": pd.get("active", False)}
