"""Virtual switch entities: one per switchable managed device (shows current on/off state).

Wallbox devices never get one — they're only read (for their power draw), never
switched by this integration.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SWITCH,
    DOMAIN,
)
from .coordinator import PVSurplusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])
    entities = [
        PVDeviceSwitch(coordinator, entry, dev)
        for dev in devices
        if not dev.get(CONF_DEVICE_IS_WALLBOX, False) and dev.get(CONF_DEVICE_SWITCH)
    ]
    async_add_entities(entities)


class PVDeviceSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Read-only mirror of the managed switch — shows PV manager's view of the device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PVSurplusCoordinator,
        entry: ConfigEntry,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device["_id"]
        self._switch_id = device[CONF_DEVICE_SWITCH]
        name = device.get(CONF_DEVICE_NAME, self._switch_id)
        prio = device.get(CONF_DEVICE_PRIORITY, 99)
        self._attr_name = f"{name} (Prio {prio})"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_managed"
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "PV Surplus Manager",
        }

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data and self.coordinator.data.device_states:
            return self.coordinator.data.device_states.get(self._device_id)
        sw = self.hass.states.get(self._switch_id)
        return sw is not None and sw.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.services.async_call(
            "switch", "turn_on", {"entity_id": self._switch_id}
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": self._switch_id}
        )
