"""Sensor platform for CUKTECH Charger - MQTT real-time."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CuktechMQTTCoordinator
from .const import DOMAIN, PORT_NAMES

_LOGGER = logging.getLogger(__name__)

PROTOCOL_OPTIONS = ["idle", "PD", "PD Fixed", "PD PPS", "USB-A", "QC", "Unknown"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up CUKTECH Charger sensors from a config entry."""
    coord = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for piid, pname in PORT_NAMES.items():
        for st in ("voltage", "current", "power"):
            entities.append(CuktechPortSensor(coord, entry, piid, pname, st))
        entities.append(CuktechPortProtocolSensor(coord, entry, piid, pname))

    entities.append(CuktechTotalPowerSensor(coord, entry))
    async_add_entities(entities)


class CuktechPortSensor(SensorEntity):
    """Sensor for CUKTECH Charger port data."""

    _attr_has_entity_name = True
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
        self.coordinator = coord
        self._entry = entry
        self._piid = piid
        self._port_name = port_name
        self._sensor_type = sensor_type
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_{sensor_type}"
        self._attr_name = f"{port_name} {sensor_type}"
        self._attr_native_unit_of_measurement = self.UNITS.get(sensor_type)
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
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return None
        return pd.get(self._sensor_type)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        pd = self.coordinator.port_data.get(str(self._piid))
        if pd is None:
            return {}
        return {"port": self._port_name, "active": pd.get("active", False)}


class CuktechTotalPowerSensor(SensorEntity):
    """Sensor for total power consumption."""

    _attr_has_entity_name = True
    _attr_name = "Total Power"
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: CuktechMQTTCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        self.coordinator = coord
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_total_power"
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
    def native_value(self) -> float:
        """Return the total power."""
        total = 0.0
        for k in ("1", "2", "3", "4"):
            pd = self.coordinator.port_data.get(k)
            if pd and pd.get("active"):
                total += pd.get("power", 0)
        return round(total, 1)


class CuktechPortProtocolSensor(SensorEntity):
    """Sensor for CUKTECH Charger port protocol."""

    _attr_has_entity_name = True
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
        self.coordinator = coord
        self._entry = entry
        self._piid = piid
        self._port_name = port_name
        self._attr_unique_id = f"{entry.entry_id}_port_{piid}_protocol"
        self._attr_name = f"{port_name} Protocol"
        self._attr_icon = "mdi:usb-c-port"
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
