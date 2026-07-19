"""Diagnostic sensors: surplus, h_battery, h_to_solar, mode, per-device power."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PRIORITY,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
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
        PVSolarCalibrationSensor(coordinator, entry),
        PVActiveSolarOffsetSensor(coordinator, entry),
    ]
    # Wallbox devices aren't evaluated in the cascade, so there's no
    # predicted-power diagnostics for them — their own power_sensor already
    # shows live power directly.
    for dev in entry.data.get(CONF_DEVICES, []):
        if not dev.get(CONF_DEVICE_IS_WALLBOX, False):
            entities.append(PVDevicePowerSensor(coordinator, entry, dev))
            entities.append(PVDeviceOffTimerSensor(coordinator, entry, dev))
    async_add_entities(entities)


class _PVSensorBase(CoordinatorEntity[PVSurplusCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def available(self) -> bool:
        # CoordinatorEntity's default ties availability to
        # last_update_success, which _require_valid() intentionally sets to
        # False for a cycle whenever a core sensor briefly blips — but the
        # coordinator still holds its last good data at that point (that's
        # the whole point of skipping the cycle instead of computing with a
        # 0). Without this override every entity here would flash
        # "unavailable" on each such blip, hiding perfectly valid last-known
        # values behind a stricter gate than the data itself needs.
        return self.coordinator.data is not None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Surplus Load Switch",
            "manufacturer": "Community",
            "model": "Surplus Load Switch",
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


class PVActiveSolarOffsetSensor(_PVSensorBase):
    """The solar-start offset (hours after sunrise) currently in effect for
    this month — whatever offsets_for() resolved to (measured/borrowed/
    default). A plain numeric sensor with a history, so you can see how the
    value in actual use has moved as calibration data accumulates, not just
    a snapshot of the calibration status."""

    _attr_name = "Aktiver Solar-Offset"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sun-clock"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_active_solar_offset"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return round(self.coordinator.data.active_solar_offset_h, 2)

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        m = dt_util.now().month
        return {
            "monat": m,
            "quelle": self.coordinator.data.calibration.get("quelle_pro_monat", {}).get(m),
        }


class PVSolarCalibrationSensor(_PVSensorBase):
    """How many of the 12 calendar months currently have a learned
    solar-start offset (vs. still falling back to the configured/default
    estimate) — see attributes for the values and how many good days each
    is based on."""

    _attr_name = "Solar-Start Kalibrierung"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_solar_calibration"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        n = len(self.coordinator.data.calibration.get("kalibrierte_monate", []))
        return f"{n}/12 Monate"

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.calibration


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
            "datenquelle": "gemessen (3 Tage)" if diag.is_measured else "geschätzt (Konfiguration)",
            "messwerte": diag.sample_count,
            "gemessener_durchschnitt_kw": round(diag.measured_avg_kw, 3) if diag.measured_avg_kw else None,
            "laufzeit_heute_h": round(diag.runtime_hours_today, 2),
            "mindest_laufzeit_erzwungen": diag.force_runtime,
            "voraussetzung_erfullt": diag.dependency_met,
            "naechster_cutoff": diag.effective_cutoff,
            "sollte_an_sein": diag.should_be_on,
            "korrekt_geschaltet": diag.is_on == diag.should_be_on,
        }

    @property
    def _diagnostics(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.device_diagnostics.get(self._device_id)


class PVDeviceOffTimerSensor(_PVSensorBase):
    """Seconds remaining before this device would actually be switched off,
    once an off-decision has started holding. There's a buffer on purpose —
    a device isn't cut the instant the battery projection turns negative;
    it has to hold for a few minutes up to ~12 (scaling with how much
    battery margin is left) before we act, so a brief dip doesn't cause an
    unnecessary switch. 0 while the device isn't currently counting down
    toward being turned off (stable on, stable off, or being force-managed
    by a window/dependency, which acts immediately with no buffer)."""

    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict) -> None:
        super().__init__(coordinator, entry)
        self._device_id = device["_id"]
        name = device.get(CONF_DEVICE_NAME, self._device_id)
        self._attr_name = f"{name} — Abschalt-Puffer"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_{self._device_id}_off_timer"

    @property
    def native_value(self):
        diag = self._diagnostics
        if diag is None or diag.off_counter <= 0:
            return 0
        remaining_cycles = max(diag.required_off_cycles - diag.off_counter, 0)
        return remaining_cycles * UPDATE_INTERVAL_SECONDS

    @property
    def extra_state_attributes(self):
        diag = self._diagnostics
        if diag is None:
            return {}
        return {
            "zyklen_gehalten": diag.off_counter,
            "benoetigte_zyklen": diag.required_off_cycles,
        }

    @property
    def _diagnostics(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.device_diagnostics.get(self._device_id)
