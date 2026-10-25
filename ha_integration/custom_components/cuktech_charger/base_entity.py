"""Base entity for CUKTECH Charger integration - reduces code duplication."""
from __future__ import annotations

from typing import Any

from homeassistant.core import callback

from .const import DEVICE_INFO, DOMAIN

CB_TYPE_PORT = "port"
CB_TYPE_SETTINGS = "settings"
CB_TYPE_ALL = "all"
CB_TYPE_CHARGE = "charge"


class CuktechBaseEntity:
    """Base entity with common lifecycle and properties for all CUKTECH entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator, entry, callback_type: str = CB_TYPE_ALL, **kwargs
    ) -> None:
        """Initialize base entity and register callback."""
        self.coordinator = coordinator
        self._entry = entry
        self._callback_type = callback_type
        super().__init__(**kwargs)
        if callback_type == CB_TYPE_PORT:
            coordinator.register_port_callback(self._update)
        elif callback_type == CB_TYPE_SETTINGS:
            coordinator.register_settings_callback(self._update)
        elif callback_type == CB_TYPE_CHARGE:
            coordinator.register_charge_event_callback(self._update)
        else:
            coordinator.register_callback(self._update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback when removed."""
        if self._callback_type == CB_TYPE_PORT:
            self.coordinator.unregister_port_callback(self._update)
        elif self._callback_type == CB_TYPE_SETTINGS:
            self.coordinator.unregister_settings_callback(self._update)
        elif self._callback_type == CB_TYPE_CHARGE:
            self.coordinator.unregister_charge_event_callback(self._update)
        else:
            self.coordinator.unregister_callback(self._update)
        await super().async_will_remove_from_hass()

    @callback
    def _update(self) -> None:
        """Handle state update."""
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info.

        设备名取 entry.title（§4.5）：多 entry = 多台充电器，用 DEVICE_INFO 里的
        固定名会让 Home Assistant 里出现一串同名设备，只能靠自动加的 _2/_3 后缀
        区分，用户根本认不出哪台是哪台。identifiers 仍是 (DOMAIN, entry_id)
        —— 换成 MAC 会让既有 entry 在设备注册表里"分裂"成新设备，历史与自动化
        全部断裂。
        """
        name = getattr(self._entry, "title", None)
        if not isinstance(name, str) or not name.strip():
            name = DEVICE_INFO["name"]
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            **self.coordinator.device_info,
            "name": name,
        }

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.available
