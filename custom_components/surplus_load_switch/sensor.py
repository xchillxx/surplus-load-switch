"""Diagnostic sensors: surplus, h_battery, h_to_solar, mode, per-device power."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
)
from .coordinator import DeviceDiagnostics, PVSurplusCoordinator
from .device_control import hub_device_info, sub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        PVSurplusSensor(coordinator, entry),
        PVBaseLoadSensor(coordinator, entry),
        PVWallboxReservedSensor(coordinator, entry),
        PVWallboxTargetSensor(coordinator, entry),
        PVBatteryFullReservedSensor(coordinator, entry),
        PVHBatterySensor(coordinator, entry),
        PVHToSolarSensor(coordinator, entry),
        PVModeSensor(coordinator, entry),
        PVSocSensor(coordinator, entry),
        PVSolarCalibrationSensor(coordinator, entry),
        PVLoadProfileSensor(coordinator, entry),
        PVActiveSolarOffsetSensor(coordinator, entry),
        PVLogTableSensor(coordinator, entry),
        PVNextCycleSensor(coordinator, entry),
    ]
    # Wallbox devices aren't evaluated in the cascade, so there's no
    # predicted-power diagnostics for them — their own power_sensor already
    # shows live power directly.
    for dev in entry.data.get(CONF_DEVICES, []):
        if dev.get(CONF_DEVICE_IS_WALLBOX, False):
            entities.append(PVWallboxMaxChargeSensor(coordinator, entry, dev))
            continue
        entities.append(PVDevicePowerSensor(coordinator, entry, dev))
        entities.append(PVDeviceSwitchCountdownSensor(coordinator, entry, dev))
    async_add_entities(entities)


class _PVSensorBase(CoordinatorEntity[PVSurplusCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def available(self) -> bool:
        # CoordinatorEntity's default ties availability to
        # last_update_success, which _get_core_float() intentionally sets to
        # False for a cycle whenever a core sensor's been unavailable for
        # longer than CORE_SENSOR_GRACE_PERIOD — but the coordinator still
        # holds its last good data at that point (that's the whole point of
        # skipping the cycle instead of computing with a 0). Without this
        # override every entity here would flash "unavailable" on each such
        # blip, hiding perfectly valid last-known values behind a stricter
        # gate than the data itself needs.
        return self.coordinator.data is not None

    @property
    def device_info(self):
        return {**hub_device_info(self._entry.entry_id), "manufacturer": "Community"}


class _PVDeviceSensorBase(_PVSensorBase):
    """Base for a sensor belonging to one configured device — lives on
    that device's own sub_device_info entry (named after the device
    itself) rather than the hub, so its friendly_name is just its own
    short label ("Ø Leistung", not "SLS Miner — Ø Leistung") and Settings
    -> Devices & Services shows one clean card per device instead of one
    giant flat list on the hub."""

    def __init__(
        self, coordinator: PVSurplusCoordinator, entry: ConfigEntry, device: dict
    ) -> None:
        super().__init__(coordinator, entry)
        self._device = device
        self._device_id = device["_id"]

    @property
    def device_info(self):
        return sub_device_info(self._entry.entry_id, self._device)


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
        # base_load_kw/solar_kw/house_load_kw/batt_ok are still read by the
        # example dashboard (as a generic fallback for installs that
        # haven't pointed those rows at their own raw sensors, and until
        # Grundlast/Akku-ausreichend's own entity_ids are known post-
        # release) — soc/min_soc/batt_kw/discharge_kw/smoothed_discharge_kw/
        # sun_above_horizon were only ever attributes here for the same
        # purpose and are no longer read anywhere (Batterie-SOC, Batterie-
        # Leistung and Sonne-über-Horizont now link straight to their real
        # source entities), so they're gone rather than kept as dead
        # duplication of data the raw sensors already provide.
        if not self.coordinator.data:
            return {}
        d = self.coordinator.data
        return {
            "base_load_kw": round(d.base_load_kw, 3),
            "solar_kw": round(d.solar_kw, 3),
            "house_load_kw": round(d.load_kw, 3),
            "batt_ok": d.batt_ok,
            "wallbox_reserviert_kw": round(d.wallbox_reserved_kw, 3),
        }


class PVBaseLoadSensor(_PVSensorBase):
    """Load the managed devices don't account for — house consumption minus
    the wallbox and whatever's currently drawn by devices this integration
    itself controls. Its own entity (not just an attribute on Überschuss)
    so it gets its own recorder history instead of always opening
    Überschuss's graph when tapped."""

    _attr_name = "Grundlast"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-import-outline"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_base_load"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.base_load_kw, 3)
        return None

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.base_load_floor


class PVWallboxReservedSensor(_PVSensorBase):
    """Sum of every wallbox's dynamic surplus reservation this cycle —
    same reasoning as PVBaseLoadSensor above: its own entity (not just
    the wallbox_reserviert_kw attribute on Überschuss, kept there too
    for convenience) so it gets its own recorder history instead of
    always opening Überschuss's graph when tapped."""

    _attr_name = "Wallbox reserviert"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ev-station"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_wallbox_reserved"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.wallbox_reserved_kw, 3)
        return None


class PVWallboxTargetSensor(_PVSensorBase):
    """The raw, pre-cap wallbox reservation rate (see
    wallbox_target_kw/_wallbox_reservation_rate) — what the wallbox would
    need at a steady pace to reach its target by the deadline, regardless
    of whether that much surplus genuinely exists right now. Verifies the
    underlying kWh/deadline math independent of current conditions, so a
    low PVWallboxReservedSensor reading next to a much higher reading
    here means the deficit math is fine — there's simply not enough
    surplus to act on it yet, not a calculation bug. Also feeds a real
    switching decision (wallbox_starved, see coordinator.py), not just
    this diagnostic — the uncapped rate is what correctly flags the
    wallbox as needing everything even on a cycle scarce enough to cap
    its own reservation down to ~0."""

    _attr_name = "Wallbox Soll-Ladeleistung"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ev-station"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_wallbox_target"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.wallbox_target_kw, 3)
        return None


class PVBatteryFullReservedSensor(_PVSensorBase):
    """The house battery's own dynamic surplus reservation this cycle —
    same reasoning as PVWallboxReservedSensor above, active whenever the
    battery is behind schedule to reach WEAK_DAY_BATTERY_FULL_SOC in
    time (see battery_full_reservation_kw in _evaluate_devices)."""

    _attr_name = "Akku reserviert"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-battery"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_battery_full_reserved"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.battery_full_reserved_kw, 3)
        return None


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


class PVLoadProfileSensor(_PVSensorBase):
    """How many (weekday, hour, house-mode) combinations have at least
    one learned daily average so far. Diagnostic only — see
    load_profile.py — doesn't affect any switching decision. The full
    learned table (per-bucket trailing daily averages) is in the
    "profil" attribute; requires a house-mode helper entity configured
    in System settings, otherwise stays at 0."""

    _attr_name = "Lastprofil Wochentag/Modus"
    _attr_icon = "mdi:calendar-clock"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_load_profile"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        n = self.coordinator.data.load_profile.get("erfasste_kombinationen", 0)
        return f"{n} Kombinationen"

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.load_profile


class PVNextCycleSensor(_PVSensorBase):
    """When the coordinator will next re-evaluate everything — a plain
    timestamp sensor, which Home Assistant renders as a live "in X Minuten"
    countdown wherever it's shown, so it's obvious the system is still
    actively checking even during a quiet stretch with nothing to log."""

    _attr_name = "Nächste Prüfung"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-refresh-outline"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_next_cycle"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.next_cycle_at


class PVLogTableSensor(_PVSensorBase):
    """Every logged event (device decisions every cycle, plus system-level
    events), newest first, as a plain list attribute — lets the dashboard
    render a real Datum/Gerät/Titel/Details table instead of the fixed
    timeline layout a logbook card is stuck with. Independent of
    coordinator.data being fresh (available regardless), since some of the
    most interesting entries — a sensor going unavailable — happen exactly
    when a cycle's data update fails."""

    _attr_name = "Log"
    _attr_icon = "mdi:table"

    @property
    def available(self) -> bool:
        return True

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_log_table"

    @property
    def native_value(self):
        return len(self.coordinator.log_entries)

    @property
    def extra_state_attributes(self):
        return {"eintraege": list(self.coordinator.log_entries)}


class PVDevicePowerSensor(_PVDeviceSensorBase):
    """Shows the predicted power for one device — measured active-runtime
    average once enough samples exist, otherwise the configured estimate."""

    _attr_name = "Ø Leistung"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-line"

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
        countdown_s, richtung = _switch_countdown(diag)
        return {
            "datenquelle": "gemessen (24h aktiv)" if diag.is_measured else "geschätzt (Konfiguration)",
            "messwerte": diag.sample_count,
            "gemessener_durchschnitt_kw": round(diag.measured_avg_kw, 3) if diag.measured_avg_kw else None,
            "laufzeit_heute_h": round(diag.runtime_hours_today, 2),
            "mindest_laufzeit_erzwungen": diag.force_runtime,
            "voraussetzung_erfullt": diag.dependency_met,
            "naechster_cutoff": diag.effective_cutoff,
            "akku_reserve_prozent": diag.device_min_soc,
            "sollte_an_sein": diag.should_be_on,
            "korrekt_geschaltet": diag.is_on == diag.should_be_on,
            "aktiviert": diag.enabled,
            "prioritaet": diag.priority,
            "ist_an": diag.is_on,
            "schalt_countdown_s": countdown_s,
            "schalt_richtung": richtung,
        }

    @property
    def _diagnostics(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.device_diagnostics.get(self._device_id)


def _switch_countdown(diag: DeviceDiagnostics) -> tuple[int, str | None]:
    """Seconds remaining before this device's pending switch action (on or
    off) actually fires, if current conditions keep holding, and which
    direction that is — (0, None) while nothing is pending (stable on,
    stable off, or being force-managed by a window/dependency, which acts
    immediately with no buffer).

    on_counter keeps pre-charging while blocked by an unmet dependency —
    a deliberate choice (see coordinator._evaluate_devices) so the device
    is ready to join *instantly* the moment its dependency clears, rather
    than waiting out a fresh hold afterward. But reaching 0 here does NOT
    by itself mean it's about to switch on if the dependency is still
    unmet at that point — the richtung text says so explicitly instead
    of reading like a plain "switches on in Xs" countdown, which was
    confirmed live to be genuinely misleading (Pool-WP showing a
    countdown the whole time its dependency was off)."""
    if diag.off_counter > 0:
        remaining_cycles = max(diag.required_off_cycles - diag.off_counter, 0)
        return remaining_cycles * UPDATE_INTERVAL_SECONDS, "ausschalten"
    if diag.on_counter > 0:
        remaining_cycles = max(diag.required_on_cycles - diag.on_counter, 0)
        richtung = "einschalten" if diag.dependency_met else "einschalten — sobald Abhängigkeit erfüllt"
        return remaining_cycles * UPDATE_INTERVAL_SECONDS, richtung
    return 0, None


class PVDeviceSwitchCountdownSensor(_PVDeviceSensorBase):
    """Seconds remaining before this device's pending switch action (on or
    off) actually fires, if current conditions hold — the direct answer to
    "why hasn't it switched yet": it's not stuck, it's still holding out
    the stability buffer (a device isn't switched the instant a
    surplus/battery decision flips; it has to hold for several minutes so
    a brief dip or spike doesn't cause an unnecessary switch). 0 while
    nothing is pending."""

    _attr_name = "Schalt-Countdown"
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_{self._device_id}_off_timer"

    @property
    def native_value(self):
        diag = self._diagnostics
        if diag is None:
            return 0
        countdown_s, _ = _switch_countdown(diag)
        return countdown_s

    @property
    def extra_state_attributes(self):
        diag = self._diagnostics
        if diag is None:
            return {}
        _, richtung = _switch_countdown(diag)
        return {
            "richtung": richtung,
            "zyklen_gehalten": diag.off_counter if richtung == "ausschalten" else diag.on_counter,
            "benoetigte_zyklen": diag.required_off_cycles if richtung == "ausschalten" else diag.required_on_cycles,
            "prioritaet": diag.priority,
        }

    @property
    def _diagnostics(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.device_diagnostics.get(self._device_id)


class PVWallboxMaxChargeSensor(_PVDeviceSensorBase):
    """Only meaningful on a wallbox device: the rolling 30-day maximum
    this wallbox's own power sensor has actually reported — the *learned*
    figure coordinator._wallbox_reserved_kw falls back to whenever the
    manual "Maximale Ladeleistung" number is left at 0. Kept as its own
    read-only sensor rather than folded into that number entity, so it's
    visible even while nobody has ever touched the manual override —
    otherwise there'd be no way to see what the system is actually using
    as the cap without reading the log or the Store file directly."""

    _attr_name = "Gelernte max. Ladeleistung"
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ev-station"

    @property
    def unique_id(self):
        return f"{self._entry.entry_id}_{self._device_id}_wallbox_max_charge"

    @property
    def native_value(self):
        diag = self._wallbox_diag
        if not diag:
            return None
        return diag.get("max_charge_kw")

    @property
    def extra_state_attributes(self):
        diag = self._wallbox_diag
        if not diag:
            return {}
        return {
            "stundenwerte": diag.get("stundenwerte"),
            "zuletzt_kalibriert": diag.get("zuletzt_kalibriert"),
        }

    @property
    def _wallbox_diag(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.wallbox_max_charge.get(self._device_id)
