"""Virtual switch entities: one per switchable managed device (shows current on/off state).

Wallbox devices never get one — they're only read (for their power draw), never
switched by this integration.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICES,
    CONF_DEVICE_ENABLED,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_MIN_DAILY_RUNTIME_H,
    CONF_DEVICE_PRICE_OPTIMIZED_FORCE,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SCHEDULE_ENTITY,
    CONF_DEVICE_STOPS_OVERNIGHT,
    CONF_DEVICE_WINDOW_END,
    DOMAIN,
)
from .coordinator import PVSurplusCoordinator
from .device_control import control_entity_id, is_device_on, sub_device_info
from .device_control import async_turn_off as _control_turn_off
from .device_control import async_turn_on as _control_turn_on


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = entry.data.get(CONF_DEVICES, [])
    non_wallbox = [dev for dev in devices if not dev.get(CONF_DEVICE_IS_WALLBOX, False)]
    entities = [
        PVDeviceSwitch(coordinator, entry, dev)
        for dev in non_wallbox
        if control_entity_id(dev)
    ]
    entities += [PVDeviceEnabledSwitch(coordinator, entry, dev) for dev in non_wallbox]
    entities += [PVDeviceStopsOvernightSwitch(coordinator, entry, dev) for dev in non_wallbox]
    entities += [
        PVDevicePriceOptimizedForceSwitch(coordinator, entry, dev)
        for dev in non_wallbox
        if dev.get(CONF_DEVICE_MIN_DAILY_RUNTIME_H)
    ]
    async_add_entities(entities)


class PVDeviceSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Read-only mirror of the managed switch or climate entity — shows PV
    manager's view of the device, regardless of how it's actually actuated."""

    _attr_has_entity_name = True

    _attr_name = "Ein/Aus"

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
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_managed"
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self):
        return sub_device_info(self._entry.entry_id, self._device)

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


class PVDeviceEnabledSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Enable/disable a device entirely. While disabled, the coordinator
    is hands-off: it never reserves cascade budget for the device and
    never actuates it either way, leaving it exactly as it is for manual
    or other-automation control — its configuration, historical power
    average, and daily-runtime data stay untouched, so it picks up right
    where it left off once re-enabled.

    Reads/writes the config entry directly rather than coordinator.data,
    since toggling it changes what the coordinator computes rather than
    reflecting something the coordinator already computed."""

    _attr_has_entity_name = True
    _attr_name = "Aktiviert"
    _attr_icon = "mdi:toggle-switch"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PVSurplusCoordinator,
        entry: ConfigEntry,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device["_id"]
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_enabled"

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
    def is_on(self) -> bool:
        dev = self._device
        return dev.get(CONF_DEVICE_ENABLED, True) if dev else True

    @property
    def extra_state_attributes(self):
        dev = self._device
        return {"prioritaet": dev.get(CONF_DEVICE_PRIORITY, 99)} if dev else {}

    async def _async_set_enabled(self, enabled: bool) -> None:
        async with self.coordinator.config_write_lock:
            devices = self._entry.data.get(CONF_DEVICES, [])
            new_devices = [
                {**d, CONF_DEVICE_ENABLED: enabled} if d.get("_id") == self._device_id else d
                for d in devices
            ]
            new_data = {**self._entry.data, CONF_DEVICES: new_devices}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_enabled(False)


class PVDeviceStopsOvernightSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Only has any effect on a device with neither a schedule.* helper nor
    a window_end configured. Such a device has no known stopping point, so
    the overnight battery projection otherwise has to assume it might keep
    drawing power all the way to solar start if switched on — which can
    make even a high-priority device fail the check outright, since its
    worst-case energy need balloons over many hours regardless of how
    little it's actually competing with other devices for that budget.

    Turning this on caps that assumption at a rolling "at most
    DEFAULT_MAX_ASSUMED_RUNTIME_H hours from right now" instead — re-
    derived every cycle, so it keeps sliding forward while conditions stay
    good rather than being a one-shot commitment — without ever forcing
    the device off by itself; it still only ever gets shed via the normal
    surplus/battery check, exactly like any other device. See
    coordinator._effective_cutoff and const.py's
    CONF_DEVICE_STOPS_OVERNIGHT/DEFAULT_MAX_ASSUMED_RUNTIME_H."""

    _attr_has_entity_name = True
    _attr_name = "Läuft nicht die ganze Nacht durch"
    _attr_icon = "mdi:weather-night"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PVSurplusCoordinator,
        entry: ConfigEntry,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device["_id"]
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_stops_overnight"

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
    def is_on(self) -> bool:
        dev = self._device
        return dev.get(CONF_DEVICE_STOPS_OVERNIGHT, False) if dev else False

    @property
    def extra_state_attributes(self):
        dev = self._device
        if not dev:
            return {}
        has_window = bool(dev.get(CONF_DEVICE_SCHEDULE_ENTITY)) or bool(dev.get(CONF_DEVICE_WINDOW_END))
        return {
            "prioritaet": dev.get(CONF_DEVICE_PRIORITY, 99),
            "wirkung": (
                "keine — Zeitfenster/Helfer bereits konfiguriert" if has_window
                else "aktiv" if dev.get(CONF_DEVICE_STOPS_OVERNIGHT, False)
                else "unbegrenzte Laufzeit angenommen"
            ),
        }

    async def _async_set(self, value: bool) -> None:
        async with self.coordinator.config_write_lock:
            devices = self._entry.data.get(CONF_DEVICES, [])
            new_devices = [
                {**d, CONF_DEVICE_STOPS_OVERNIGHT: value} if d.get("_id") == self._device_id else d
                for d in devices
            ]
            new_data = {**self._entry.data, CONF_DEVICES: new_devices}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class PVDevicePriceOptimizedForceSwitch(CoordinatorEntity[PVSurplusCoordinator], SwitchEntity):
    """Only meaningful on a device with a configured minimum daily
    runtime target — see CONF_DEVICE_PRICE_OPTIMIZED_FORCE and
    coordinator._price_optimized_force_active. When on, once forcing
    becomes necessary at all (the target is genuinely at risk of being
    missed), the still-missing hours are scheduled into the cheapest
    remaining Tibber price slots before the device's own window closes,
    instead of forcing continuously from the moment the trigger fires.
    Requires the device to have a real schedule/window configured — a
    windowless device has no fixed deadline to schedule slots against,
    so this switch has no effect for one regardless of its state (see
    extra_state_attributes below). Off by default: unconditional
    forcing the instant it's needed remains the simpler, safer default
    for anyone not specifically opting into price awareness."""

    _attr_has_entity_name = True
    _attr_name = "Erzwungene Mindest-Laufzeit preisoptimiert"
    _attr_icon = "mdi:currency-eur"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PVSurplusCoordinator,
        entry: ConfigEntry,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = device["_id"]
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_price_optimized_force"

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
    def is_on(self) -> bool:
        dev = self._device
        return dev.get(CONF_DEVICE_PRICE_OPTIMIZED_FORCE, False) if dev else False

    @property
    def extra_state_attributes(self):
        dev = self._device
        if not dev:
            return {}
        has_window = bool(dev.get(CONF_DEVICE_SCHEDULE_ENTITY)) or bool(dev.get(CONF_DEVICE_WINDOW_END))
        return {
            "prioritaet": dev.get(CONF_DEVICE_PRIORITY, 99),
            "wirkung": (
                "aktiv" if dev.get(CONF_DEVICE_PRICE_OPTIMIZED_FORCE, False) and has_window
                else "keine — kein Zeitfenster/Helfer konfiguriert" if not has_window
                else "inaktiv"
            ),
        }

    async def _async_set(self, value: bool) -> None:
        async with self.coordinator.config_write_lock:
            devices = self._entry.data.get(CONF_DEVICES, [])
            new_devices = [
                {**d, CONF_DEVICE_PRICE_OPTIMIZED_FORCE: value} if d.get("_id") == self._device_id else d
                for d in devices
            ]
            new_data = {**self._entry.data, CONF_DEVICES: new_devices}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)
