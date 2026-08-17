"""Select entities: entity-reference and fixed-choice config fields.

These replace the entity-selector dropdowns in the native Configure
dialog with equivalent live, dashboard-editable controls — same idea as
number.py, but for fields whose value is itself another entity_id (or one
of a small fixed set of choices) rather than a plain number. Options are
computed live from the current entity registry rather than cached, so a
newly-created helper (e.g. a schedule you just added) shows up without
reloading anything.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CLIMATE_HVAC_MODE_OPTIONS,
    CONF_BATT_SENSOR,
    CONF_DEVICES,
    CONF_DEVICE_CLIMATE_ENTITY,
    CONF_DEVICE_CLIMATE_ON_MODE,
    CONF_DEVICE_DEPENDS_ON,
    CONF_DEVICE_IS_CLIMATE,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SCHEDULE_ENTITY,
    CONF_DEVICE_SWITCH,
    CONF_LOAD_SENSOR,
    CONF_SOC_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_WALLBOX_CAPACITY_ENTITY,
    CONF_WALLBOX_SOC_ENTITY,
    CONF_WALLBOX_PRESENT_ENTITY,
    CONF_WALLBOX_TARGET_SOC_ENTITY,
    DOMAIN,
    SELECT_NONE,
)
from .coordinator import PVSurplusCoordinator
from .device_control import hub_device_info, sub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [
        PVGlobalSensorSelect(coordinator, entry, CONF_SOLAR_SENSOR, "Solar-Sensor"),
        PVGlobalSensorSelect(coordinator, entry, CONF_LOAD_SENSOR, "Last-Sensor"),
        PVGlobalSensorSelect(coordinator, entry, CONF_SOC_SENSOR, "SOC-Sensor"),
        PVGlobalSensorSelect(coordinator, entry, CONF_BATT_SENSOR, "Akku-Leistungssensor"),
    ]
    devices = entry.data.get(CONF_DEVICES, [])
    non_wallbox = [d for d in devices if not d.get(CONF_DEVICE_IS_WALLBOX, False)]
    for dev in non_wallbox:
        entities.append(PVDeviceControlEntitySelect(coordinator, entry, dev))
        if dev.get(CONF_DEVICE_IS_CLIMATE, False):
            entities.append(PVDeviceClimateOnModeSelect(coordinator, entry, dev))
        entities.append(PVDevicePowerSensorSelect(coordinator, entry, dev))
        entities.append(PVDeviceScheduleSelect(coordinator, entry, dev))
        # Candidates include wallboxes too — a wallbox is never itself
        # switched by the cascade, but another device can still depend on
        # it (see coordinator._wallbox_satisfied): "don't run until the
        # car's satisfied or the wallbox has been idle a while".
        entities.append(PVDeviceDependsOnSelect(coordinator, entry, dev, devices))
    for dev in devices:
        if dev.get(CONF_DEVICE_IS_WALLBOX, False):
            entities.append(PVWallboxCapacitySelect(coordinator, entry, dev))
            entities.append(PVWallboxSocSelect(coordinator, entry, dev))
            entities.append(PVWallboxTargetSocSelect(coordinator, entry, dev))
            entities.append(PVWallboxPresentSelect(coordinator, entry, dev))
    async_add_entities(entities)


class _PVSelectBase(CoordinatorEntity[PVSurplusCoordinator], SelectEntity):
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


class PVGlobalSensorSelect(_PVSelectBase):
    """One of the four core global sensors (solar/load/SOC/battery power).
    Picking an invalid one is caught the same way any bad sensor already
    is — the coordinator's sensor-outage safety skips the cycle rather
    than acting on it — no extra validation needed here."""

    _attr_icon = "mdi:swap-horizontal"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, field: str, name: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._field = field
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{field}_select"

    @property
    def options(self) -> list[str]:
        live = sorted(self.hass.states.async_entity_ids("sensor"))
        current = self.current_option
        if current and current not in live:
            live = sorted([*live, current])
        return live

    @property
    def current_option(self) -> str | None:
        return self._entry.data.get(self._field)

    async def async_select_option(self, option: str) -> None:
        async with self.coordinator.config_write_lock:
            new_data = {**self._entry.data, self._field: option}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)


class _PVDeviceSelectBase(_PVSelectBase):
    """Shared device lookup + write plumbing for a per-device select.
    Subclasses set _field to the const.py config key they edit."""

    _field: str = ""

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry)
        self._device_id = device["_id"]

    @property
    def device_info(self):
        return sub_device_info(self._entry.entry_id, self._device or {"_id": self._device_id})

    @property
    def _device(self) -> dict | None:
        devices = self._entry.data.get(CONF_DEVICES, [])
        return next((d for d in devices if d.get("_id") == self._device_id), None)

    @property
    def extra_state_attributes(self):
        dev = self._device
        return {"prioritaet": dev.get(CONF_DEVICE_PRIORITY, 99)} if dev else {}

    @property
    def current_option(self) -> str | None:
        dev = self._device
        return dev.get(self._field) if dev else None

    async def _async_write(self, value) -> None:
        async with self.coordinator.config_write_lock:
            devices = self._entry.data.get(CONF_DEVICES, [])
            new_devices = [
                {**d, self._field: value} if d.get("_id") == self._device_id else d
                for d in devices
            ]
            new_data = {**self._entry.data, CONF_DEVICES: new_devices}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)

    async def async_select_option(self, option: str) -> None:
        await self._async_write(option)


class PVDeviceControlEntitySelect(_PVDeviceSelectBase):
    """Which real entity this device controls — a switch for a normal
    device, a climate entity for a climate-controlled one."""

    _attr_icon = "mdi:electric-switch"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        is_climate = device.get(CONF_DEVICE_IS_CLIMATE, False)
        self._field = CONF_DEVICE_CLIMATE_ENTITY if is_climate else CONF_DEVICE_SWITCH
        self._domain = "climate" if is_climate else "switch"
        self._attr_name = "Steuerungs-Entität"
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_control_entity"

    @property
    def options(self) -> list[str]:
        live = sorted(self.hass.states.async_entity_ids(self._domain))
        current = self.current_option
        if current and current not in live:
            live = sorted([*live, current])
        return live


class PVDeviceClimateOnModeSelect(_PVDeviceSelectBase):
    """Which hvac_mode counts as "on" for a climate-controlled device
    (e.g. "heat" for a pool heat pump). Only added for devices configured
    as climate-controlled."""

    _field = CONF_DEVICE_CLIMATE_ON_MODE
    _attr_name = "Climate-Modus (An)"
    _attr_icon = "mdi:thermostat"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_climate_on_mode"

    @property
    def options(self) -> list[str]:
        return CLIMATE_HVAC_MODE_OPTIONS

    @property
    def current_option(self) -> str:
        dev = self._device
        return dev.get(self._field, "heat") if dev else "heat"


class _PVDeviceOptionalEntitySelect(_PVDeviceSelectBase):
    """Shared logic for an optional per-device entity-reference field:
    any entity in _domain, or the SELECT_NONE sentinel to clear it."""

    _domain: str | tuple[str, ...] = ""

    @property
    def options(self) -> list[str]:
        live = sorted(self.hass.states.async_entity_ids(self._domain))
        dev = self._device
        current = dev.get(self._field) if dev else None
        if current and current not in live:
            live = sorted([*live, current])
        return [SELECT_NONE, *live]

    @property
    def current_option(self) -> str:
        dev = self._device
        val = dev.get(self._field) if dev else None
        return val or SELECT_NONE

    async def async_select_option(self, option: str) -> None:
        await self._async_write(None if option == SELECT_NONE else option)


class PVDevicePowerSensorSelect(_PVDeviceOptionalEntitySelect):
    _field = CONF_DEVICE_POWER_SENSOR
    _domain = "sensor"
    _attr_name = "Leistungssensor"
    _attr_icon = "mdi:flash-outline"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_power_sensor_select"


class PVDeviceScheduleSelect(_PVDeviceOptionalEntitySelect):
    _field = CONF_DEVICE_SCHEDULE_ENTITY
    _domain = "schedule"
    _attr_name = "Zeitplan-Helfer"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_schedule_select"


class PVWallboxCapacitySelect(_PVDeviceOptionalEntitySelect):
    """Only meaningful on a wallbox device: the car's total usable
    battery capacity, as an entity reference rather than a plain number
    — several EV/charging integrations (e.g. a spot-price charge
    scheduler) already track and recalibrate this themselves, so
    pointing at that existing entity means it's never re-entered by
    hand or goes stale here when the source recalibrates. Together with
    the SOC/target-SOC entities below, lets
    coordinator._wallbox_reserved_kw work out how many kWh are still
    missing to the charge target. number or sensor domain, since
    capacity trackers show up as either depending on the integration."""

    _field = CONF_WALLBOX_CAPACITY_ENTITY
    _domain = ("number", "sensor")
    _attr_name = "Akkukapazität-Entität Auto"
    _attr_icon = "mdi:car-battery"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_capacity_select"


class PVWallboxSocSelect(_PVDeviceOptionalEntitySelect):
    """Only meaningful on a wallbox device: the car's own current-SOC
    sensor (e.g. from a Tesla/EV integration) — together with the
    target-SOC entity below and the capacity entity above, lets
    coordinator._wallbox_reserved_kw compute how many kWh are still
    missing to the charge target."""

    _field = CONF_WALLBOX_SOC_ENTITY
    _domain = "sensor"
    _attr_name = "SOC-Sensor Auto"
    _attr_icon = "mdi:battery-charging-70"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_soc_select"


class PVWallboxTargetSocSelect(_PVDeviceOptionalEntitySelect):
    """Only meaningful on a wallbox device: the car's charge-limit/
    target-SOC sensor (e.g. from a Tesla/EV integration) — see
    PVWallboxSocSelect above."""

    _field = CONF_WALLBOX_TARGET_SOC_ENTITY
    _domain = "sensor"
    _attr_name = "Ladeziel-Sensor Auto"
    _attr_icon = "mdi:battery-charging-100"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_target_soc_select"


class PVWallboxPresentSelect(_PVDeviceOptionalEntitySelect):
    """Only meaningful on a wallbox device: a binary_sensor ("plugged
    in") or device_tracker (the car itself) that tells
    coordinator._wallbox_reserved_kw whether the car is actually there
    to charge — without this, the SOC/target-SOC entities only ever
    hold the last reading from whenever the car left, so the reservation
    would happily hold surplus back for a car that's nowhere nearby.
    Unset disables the presence check entirely (matches the prior
    behavior for installs that don't need one)."""

    _field = CONF_WALLBOX_PRESENT_ENTITY
    _domain = ("binary_sensor", "device_tracker")
    _attr_name = "Anwesenheits-Entität Auto"
    _attr_icon = "mdi:car-connected"

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_wallbox_present_select"


class PVDeviceDependsOnSelect(_PVDeviceSelectBase):
    """Which other configured device (if any) must already be on before
    this one may run. Options are the *other* devices' own configured
    names, not raw entity IDs, since these aren't live HA entities."""

    _field = CONF_DEVICE_DEPENDS_ON
    _attr_name = "Abhängigkeit"
    _attr_icon = "mdi:link-variant"

    def __init__(
        self,
        coordinator: PVSurplusCoordinator,
        entry: ConfigEntry,
        device: dict,
        candidates: list[dict],
    ) -> None:
        super().__init__(coordinator, entry, device)
        self._attr_unique_id = f"{entry.entry_id}_{self._device_id}_depends_on"
        self._choices = {
            d.get(CONF_DEVICE_NAME, d["_id"]): d["_id"]
            for d in candidates
            if d["_id"] != self._device_id
        }

    @property
    def options(self) -> list[str]:
        return [SELECT_NONE, *sorted(self._choices)]

    @property
    def current_option(self) -> str:
        dev = self._device
        target_id = dev.get(self._field) if dev else None
        if not target_id:
            return SELECT_NONE
        for label, did in self._choices.items():
            if did == target_id:
                return label
        return SELECT_NONE

    async def async_select_option(self, option: str) -> None:
        await self._async_write(None if option == SELECT_NONE else self._choices.get(option))
