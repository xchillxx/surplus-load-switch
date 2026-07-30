"""Number entities: global settings + per-device tunable numeric values.

Every entity here reads/writes the config entry directly rather than
coordinator.data, since the value being edited *is* the config, not
something the coordinator computed from it. Writing triggers a full
integration reload via the config entry's update listener (same as
changing anything through the native Configure dialog) — the value
takes effect on the next cycle.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_MIN_DAILY_RUNTIME_H,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_PRIORITY,
    CONF_MIN_SOC,
    DOMAIN,
)
from .coordinator import PVSurplusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        PVMinSocNumber(coordinator, entry),
        PVBatteryCapacityNumber(coordinator, entry),
    ]
    for dev in entry.data.get(CONF_DEVICES, []):
        if dev.get(CONF_DEVICE_IS_WALLBOX, False):
            continue
        entities.append(PVDevicePriorityNumber(coordinator, entry, dev))
        entities.append(PVDevicePowerEstimateNumber(coordinator, entry, dev))
        entities.append(PVDeviceMinRuntimeNumber(coordinator, entry, dev))
    async_add_entities(entities)


class _PVGlobalNumberBase(CoordinatorEntity[PVSurplusCoordinator], NumberEntity):
    """Shared behavior for global (non-per-device) config number entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Surplus Load Switch",
        }

    @property
    def available(self) -> bool:
        return True


class PVMinSocNumber(_PVGlobalNumberBase):
    _attr_name = "Mindest-SOC"
    _attr_icon = "mdi:battery-low"
    _attr_native_min_value = 5
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_min_soc"

    @property
    def native_value(self) -> float:
        return self._entry.data.get(CONF_MIN_SOC, 20.0)

    async def async_set_native_value(self, value: float) -> None:
        new_data = {**self._entry.data, CONF_MIN_SOC: value}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        # Update coordinator config live in addition to the reload the
        # entry-update listener triggers, so the currently-in-flight cycle
        # (if any) already sees it rather than waiting for the reload.
        self.coordinator._config[CONF_MIN_SOC] = value
        await self.coordinator.async_request_refresh()


class PVBatteryCapacityNumber(_PVGlobalNumberBase):
    _attr_name = "Batteriekapazität"
    _attr_icon = "mdi:battery-high"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_battery_capacity"

    @property
    def native_value(self) -> float:
        return self._entry.data.get(CONF_BATTERY_CAPACITY_KWH, 13.8)

    async def async_set_native_value(self, value: float) -> None:
        new_data = {**self._entry.data, CONF_BATTERY_CAPACITY_KWH: value}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)


class _PVDeviceNumberBase(CoordinatorEntity[PVSurplusCoordinator], NumberEntity):
    """Shared read/write plumbing for a per-device tunable numeric config
    field. Subclasses set _field to the const.py config key they edit."""

    _attr_has_entity_name = True
    _field: str = ""

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device["_id"]

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Surplus Load Switch",
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def _device(self) -> dict | None:
        devices = self._entry.data.get(CONF_DEVICES, [])
        return next((d for d in devices if d.get("_id") == self._device_id), None)

    @property
    def extra_state_attributes(self):
        dev = self._device
        return {"prioritaet": dev.get(CONF_DEVICE_PRIORITY, 99)} if dev else {}

    async def _async_write(self, value) -> None:
        devices = self._entry.data.get(CONF_DEVICES, [])
        new_devices = [
            {**d, self._field: value} if d.get("_id") == self._device_id else d
            for d in devices
        ]
        new_data = {**self._entry.data, CONF_DEVICES: new_devices}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)


class PVDevicePriorityNumber(_PVDeviceNumberBase):
    _field = CONF_DEVICE_PRIORITY
    _attr_icon = "mdi:sort-numeric-ascending"
    _attr_native_min_value = 1
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        name = device.get(CONF_DEVICE_NAME, self._device_id)
        self._attr_name = f"{name} — Priorität"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_priority"

    @property
    def native_value(self) -> float:
        dev = self._device
        return dev.get(CONF_DEVICE_PRIORITY, 99) if dev else 99

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(int(value))


class PVDevicePowerEstimateNumber(_PVDeviceNumberBase):
    """The configured estimate — only actually used until enough measured
    samples exist (see power_tracker.py); editing it has no effect once a
    power sensor has taken over, beyond being the fallback again if that
    sensor's history is ever cleared."""

    _field = CONF_DEVICE_POWER_KW
    _attr_icon = "mdi:flash"
    _attr_native_min_value = 0.05
    _attr_native_max_value = 22.0
    _attr_native_step = 0.05
    _attr_native_unit_of_measurement = "kW"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        name = device.get(CONF_DEVICE_NAME, self._device_id)
        self._attr_name = f"{name} — Geschätzte Leistung"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_power_estimate"

    @property
    def native_value(self) -> float:
        dev = self._device
        return dev.get(CONF_DEVICE_POWER_KW, 0.15) if dev else 0.15

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value)


class PVDeviceMinRuntimeNumber(_PVDeviceNumberBase):
    """0 means "not set" (the feature is off) — config_flow's own field is
    optional/None, but a number entity can't represent that, so 0 is the
    sentinel here."""

    _field = CONF_DEVICE_MIN_DAILY_RUNTIME_H
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 24
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        name = device.get(CONF_DEVICE_NAME, self._device_id)
        self._attr_name = f"{name} — Mindest-Laufzeit (0 = aus)"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_min_runtime"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_DEVICE_MIN_DAILY_RUNTIME_H) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)
