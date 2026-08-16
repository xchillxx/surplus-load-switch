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
    CONF_DEVICE_MIN_SOC_PERCENT,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_PRIORITY,
    CONF_MIN_SOC,
    CONF_WALLBOX_BATTERY_CAPACITY_KWH,
    CONF_WALLBOX_MAX_CHARGE_KW,
    CONF_WALLBOX_SATISFIED_KW,
    CONF_WALLBOX_WEAK_DAY_PRIORITY,
    DOMAIN,
)
from .coordinator import PVSurplusCoordinator
from .device_control import hub_device_info, sub_device_info


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
            entities.append(PVWallboxSatisfiedKwNumber(coordinator, entry, dev))
            entities.append(PVWallboxWeakDayPriorityNumber(coordinator, entry, dev))
            entities.append(PVWallboxBatteryCapacityNumber(coordinator, entry, dev))
            entities.append(PVWallboxMaxChargeKwNumber(coordinator, entry, dev))
            continue
        entities.append(PVDevicePriorityNumber(coordinator, entry, dev))
        entities.append(PVDevicePowerEstimateNumber(coordinator, entry, dev))
        entities.append(PVDeviceMinRuntimeNumber(coordinator, entry, dev))
        entities.append(PVDeviceMinSocNumber(coordinator, entry, dev))
    async_add_entities(entities)


class _PVGlobalNumberBase(CoordinatorEntity[PVSurplusCoordinator], NumberEntity):
    """Shared behavior for global (non-per-device) config number entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

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
        async with self.coordinator.config_write_lock:
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
        async with self.coordinator.config_write_lock:
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
        return sub_device_info(self._entry.entry_id, self._device or {"_id": self._device_id})

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
        async with self.coordinator.config_write_lock:
            devices = self._entry.data.get(CONF_DEVICES, [])
            new_devices = [
                {**d, self._field: value} if d.get("_id") == self._device_id else d
                for d in devices
            ]
            new_data = {**self._entry.data, CONF_DEVICES: new_devices}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)


class PVDevicePriorityNumber(_PVDeviceNumberBase):
    _field = CONF_DEVICE_PRIORITY
    _attr_name = "Priorität"
    _attr_icon = "mdi:sort-numeric-ascending"
    _attr_native_min_value = 1
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
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
    _attr_name = "Geschätzte Leistung"
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
    _attr_name = "Mindest-Laufzeit (0 = aus)"
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
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_min_runtime"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_DEVICE_MIN_DAILY_RUNTIME_H) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)


class PVDeviceMinSocNumber(_PVDeviceNumberBase):
    """Hard floor: below this battery SOC, an already-on device is
    forced off — a reserve-protection cutoff, not an on/off
    precondition (turning on via direct PV surplus is unaffected). 0
    means "not set" (no device-specific floor beyond the global
    Mindest-SOC), same sentinel convention as PVDeviceMinRuntimeNumber.
    Suspended while a minimum daily runtime target is being
    force-enforced — see coordinator._evaluate_devices and const.py's
    CONF_DEVICE_MIN_SOC_PERCENT for the full reasoning."""

    _field = CONF_DEVICE_MIN_SOC_PERCENT
    _attr_name = "Akku-Reserve — Gerät aus unter (0 = aus)"
    _attr_icon = "mdi:battery-alert-variant-outline"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_min_soc"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_DEVICE_MIN_SOC_PERCENT) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)


class PVWallboxSatisfiedKwNumber(_PVDeviceNumberBase):
    """Only meaningful on a wallbox device: once its own charging power
    reaches this, another device may depend on it being "satisfied" —
    it's getting plenty, no reason to keep holding a lower-priority
    device back purely for it. Entirely optional: even with this left at
    0 ("not set"), a device depending on a wallbox still gets released
    once the wallbox has been idle (near-zero power) for the standard
    hold time — see coordinator._wallbox_satisfied."""

    _field = CONF_WALLBOX_SATISFIED_KW
    _attr_name = "Ausreichende Ladeleistung (0 = aus)"
    _attr_icon = "mdi:ev-station"
    _attr_native_min_value = 0
    _attr_native_max_value = 22
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kW"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_satisfied_kw"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_WALLBOX_SATISFIED_KW) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)


class PVWallboxWeakDayPriorityNumber(_PVDeviceNumberBase):
    """Only meaningful on a wallbox device: this wallbox's effective
    priority on a detected weak day, even though it's never itself
    switched or ranked by the cascade otherwise. Any candidate device
    whose own priority is this number or worse (higher) gets held off
    entirely on that day, until the battery's nearly full — the wallbox
    takes over that priority slot for the day (a device at the *same*
    priority number counts as behind it, not tied with it), the car gets
    first claim on a scarce day and everything from there on down waits.
    0 ("not set") disables this for the wallbox; it needs the solar-start
    calibration to actually have a reference peak for the current month
    before it can do anything either way, so it's off by default even
    once set on a fresh or not-yet-calibrated install."""

    _field = CONF_WALLBOX_WEAK_DAY_PRIORITY
    _attr_name = "Schwacher-Tag-Priorität (0 = aus)"
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_native_min_value = 0
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_weak_day_priority"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_WALLBOX_WEAK_DAY_PRIORITY) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(int(value) if value > 0 else None)


class PVWallboxBatteryCapacityNumber(_PVDeviceNumberBase):
    """Only meaningful on a wallbox device: the car's total usable
    battery capacity (kWh) — together with the SOC/target-SOC entities
    (select.py), lets coordinator._wallbox_reserved_kw work out how many
    kWh are still missing to the charge target and, from there, the kW
    the wallbox needs reserved from the current surplus (see
    CONF_WALLBOX_BATTERY_CAPACITY_KWH in const.py). 0 ("not set")
    disables the whole dynamic-reservation feature for this wallbox,
    same sentinel convention as every other optional per-device number
    here — the SOC/target-SOC entities being unset does the same."""

    _field = CONF_WALLBOX_BATTERY_CAPACITY_KWH
    _attr_name = "Akkukapazität Auto (0 = aus)"
    _attr_icon = "mdi:car-battery"
    _attr_native_min_value = 0
    _attr_native_max_value = 200
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_battery_capacity_kwh"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_WALLBOX_BATTERY_CAPACITY_KWH) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)


class PVWallboxMaxChargeKwNumber(_PVDeviceNumberBase):
    """Only meaningful on a wallbox device: caps
    coordinator._wallbox_reserved_kw at whatever the car/charger can
    actually draw, regardless of how large the remaining deficit is —
    no point reserving surplus the wallbox could never use. 0 ("not
    set") means no cap beyond whatever surplus genuinely exists."""

    _field = CONF_WALLBOX_MAX_CHARGE_KW
    _attr_name = "Maximale Ladeleistung (0 = aus)"
    _attr_icon = "mdi:ev-station"
    _attr_native_min_value = 0
    _attr_native_max_value = 43
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kW"
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_max_charge_kw"

    @property
    def native_value(self) -> float:
        dev = self._device
        return (dev.get(CONF_WALLBOX_MAX_CHARGE_KW) or 0) if dev else 0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value if value > 0 else None)
