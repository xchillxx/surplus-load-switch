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
    DOMAIN,
)
from .coordinator import PVSurplusCoordinator
from .device_control import control_entity_id, is_device_on
from .device_control import async_turn_off as _control_turn_off
from .device_control import async_turn_on as _control_turn_on


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])
    entities = [
        PVDeviceSwitch(coordinator, entry, dev)
        for dev in devices
        if not dev.get(CONF_DEVICE_IS_WALLBOX, False) and control_entity_id(dev)
    ]
    async_add_entities(entities)


class PVDeviceSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Read-only mirror of the managed switch or climate entity — shows PV
    manager's view of the device, regardless of how it's actually actuated."""

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
        self._device = device
        name = device.get(CONF_DEVICE_NAME, control_entity_id(device))
        prio = device.get(CONF_DEVICE_PRIORITY, 99)
        self._attr_name = f"{name} (Prio {prio})"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_managed"
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Surplus Load Switch",
        }

    @property
    def available(self) -> bool:
        # See _PVSensorBase.available in sensor.py — a coordinator refresh
        # skipped due to a transient sensor blip shouldn't hide this
        # entity's last-known state.
        return self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data and self.coordinator.data.device_states:
            return self.coordinator.data.device_states.get(self._device_id)
        return is_device_on(self.hass, self._device)

    async def async_turn_on(self, **kwargs) -> None:
        await _control_turn_on(self.hass, self._device)

    async def async_turn_off(self, **kwargs) -> None:
        await _control_turn_off(self.hass, self._device)
