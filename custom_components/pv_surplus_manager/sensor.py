"""Diagnostic sensors: surplus, h_battery, h_to_solar, mode, per-device power."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, CONF_DEVICE_IS_WALLBOX, CONF_DEVICE_NAME, CONF_DEVICE_PRIORITY, DOMAIN
from .coordinator import PVSurplusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        PVSurplusSensor(coordinator, entry),
        PVHBatterySensor(coordinator, entry),
        PVHToSolarSensor(coordinator, entry),
        PVModeSensor(coordinator, entry),
        PVSocSensor(coordinator, entry),
    ]
    # Wallbox devices aren't evaluated in the cascade, so there's no
    # predicted-power diagnostics for them — their own power_sensor already
    # shows live power directly.
    for dev in entry.data.get(CONF_DEVICES, []):
        if not dev.get(CONF_DEVICE_IS_WALLBOX, False):
            entities.append(PVDevicePowerSensor(coordinator, entry, dev))
    async_add_entities(entities)


class _PVSensorBase(CoordinatorEntity[PVSurplusCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "PV Surplus Manager",
            "manufacturer": "Community",
            "model": "PV Surplus Manager",
        }


class PVSurplusSensor(_PVSensorBase):
    _attr_name = "Überschuss"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_surplus"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.surplus_kw, 3)
        return None

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        d = self.coordinator.data
        return {
            "base_load_kw": round(d.base_load_kw, 3),
            "solar_kw": round(d.solar_kw, 3),
            "house_load_kw": round(d.load_kw, 3),
        }


class PVHBatterySensor(_PVSensorBase):
    _attr_name = "Akku reicht"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-clock"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_h_battery"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return round(min(self.coordinator.data.h_battery, 999), 1)


class PVHToSolarSensor(_PVSensorBase):
    _attr_name = "Bis Solar-Start"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:weather-sunny-alert"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_h_to_solar"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.h_to_solar, 2)


class PVModeSensor(_PVSensorBase):
    _attr_name = "Modus"
    _attr_icon = "mdi:information-outline"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_mode"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return "Unbekannt"
        d = self.coordinator.data
        if d.solar_kw > 0.5:
            if d.surplus_kw > 0.2:
                return "Tagmodus — Überschuss"
            return "Tagmodus — Wolken"
        if d.batt_ok:
            return "Nachtmodus — Akku OK"
        return "Sparmodus — Akku schont"


class PVSocSensor(_PVSensorBase):
    _attr_name = "Verfügbare Akkukapazität"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_avail_kwh"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.avail_kwh, 2)


class PVDevicePowerSensor(_PVSensorBase):
    """Shows the predicted power for one device — measured 7-day average once
    enough samples exist, otherwise the configured estimate."""

    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict) -> None:
        super().__init__(coordinator, entry)
        self._device_id = device["_id"]
        name = device.get(CONF_DEVICE_NAME, self._device_id)
        self._attr_name = f"{name} — Ø Leistung"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_{self._device_id}_power"

    @property
    def native_value(self):
        diag = self._diagnostics
        if diag is None:
            return None
        return round(diag.predicted_power_kw, 3)

    @property
    def extra_state_attributes(self):
        diag = self._diagnostics
        if diag is None:
            return {}
        return {
            "datenquelle": "gemessen (7 Tage)" if diag.is_measured else "geschätzt (Konfiguration)",
            "messwerte": diag.sample_count,
            "gemessener_durchschnitt_kw": round(diag.measured_avg_kw, 3) if diag.measured_avg_kw else None,
        }

    @property
    def _diagnostics(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.device_diagnostics.get(self._device_id)
