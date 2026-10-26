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

        设备名取 `coordinator.display_name`（= 从服务端同步来的 `devices[].name`），
        取不到才回落到 entry.title。多 entry = 多台充电器，用 DEVICE_INFO 里的固定名
        会让 Home Assistant 里出现一串同名设备，只能靠自动加的 _2/_3 后缀区分。
        identifiers 仍是 (DOMAIN, entry_id) —— 换成 MAC 会让既有 entry 在设备注册表里
        "分裂"成新设备，历史与自动化全部断裂。
        """
        # 名字优先取 coordinator 同步到的服务端设备名（`display_name`），取不到才
        # 回落到 entry title：entry title 是"集成条目/中枢"的标签，用户可能把它
        # 固定成任意名字，不该拿它当设备名用。
        name = getattr(self.coordinator, "display_name", None)
        if not isinstance(name, str) or not name.strip():
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
