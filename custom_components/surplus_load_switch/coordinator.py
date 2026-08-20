"""Core logic: evaluates PV surplus and manages device switching.

Devices are switched in priority order using a cascade: the highest-priority
device gets first claim on available surplus, the next device only sees what's
left over after that, and so on. Each device's power need is either measured
(7-day rolling average while it's ON, see power_tracker.py) or, until enough
samples exist, the configured estimate.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_FULL_TARGET_TIME_BUFFER_H,
    BATT_OK_BUFFER_H,
    CALIBRATION_INTERVAL_HOURS,
    CONF_BATT_SENSOR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICES,
    CONF_DEVICE_DEPENDS_ON,
    CONF_DEVICE_ENABLED,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_MIN_DAILY_RUNTIME_H,
    CONF_DEVICE_MIN_SOC_PERCENT,
    CONF_DEVICE_NAME,
    CONF_DEVICE_OFF_ONLY,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SCHEDULE_ENTITY,
    CONF_DEVICE_STOPS_OVERNIGHT,
    CONF_DEVICE_WINDOW_END,
    CONF_DEVICE_WINDOW_START,
    CONF_HAUSMODUS_ENTITY,
    CONF_LOAD_SENSOR,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    CONF_SOLAR_FORECAST_REMAINING_ENTITY,
    CONF_SOLAR_OFFSETS,
    CONF_SOLAR_SENSOR,
    CONF_WALLBOX_CAPACITY_ENTITY,
    CONF_WALLBOX_MAX_CHARGE_KW,
    CONF_WALLBOX_PRESENT_ENTITY,
    CONF_WALLBOX_SATISFIED_KW,
    CONF_WALLBOX_SOC_ENTITY,
    CONF_WALLBOX_TARGET_SOC_ENTITY,
    CORE_SENSOR_GRACE_PERIOD,
    DAYTIME_PROJECTION_HORIZON_H,
    DEFAULT_MAX_ASSUMED_RUNTIME_H,
    DEFAULT_SOLAR_OFFSETS,
    DISCHARGE_SMOOTHING_SAMPLES,
    DOMAIN,
    LOAD_SENSOR_STALENESS_GRACE,
    MARGIN_FOR_MAX_PATIENCE_H,
    MAX_BATTERY_OPTIMIZATION_DEVICES,
    MIN_RUNTIME_FORCE_AFTER_HOUR,
    FORCE_RUNTIME_FORECAST_SHARE_MAX,
    MIN_RUNTIME_FORCE_BUFFER_H,
    MIN_SAMPLES_FOR_MEASURED_AVG,
    OFF_CYCLES_FLOOR,
    RE_INCLUSION_COMFORT_BUFFER_H,
    STABLE_OFF_CYCLES,
    STABLE_OFF_CYCLES_MAX,
    STABLE_ON_CYCLES,
    STAGGER_CYCLES_PER_PRIORITY_STEP,
    STALENESS_MIN_REFRESHES,
    SOLAR_START_MIN_KW,
    SURPLUS_OFF_THRESHOLD,
    SURPLUS_ON_THRESHOLD,
    UPDATE_INTERVAL_SECONDS,
    WALLBOX_IDLE_THRESHOLD_KW,
    WALLBOX_FORECAST_MIN_KWH,
    WALLBOX_RELIEF_CYCLES,
    WALLBOX_TARGET_MIN_HOURS,
    WALLBOX_TARGET_TIME_BUFFER_H,
    WEAK_DAY_BATTERY_FULL_SOC,
)
from .device_control import async_turn_off, async_turn_on, control_entity_id, is_device_on
from .power_tracker import DevicePowerTracker
from .runtime_tracker import DailyRuntimeTracker
from .base_load_floor import BaseLoadFloorCalibrator
from .wallbox_charge_calibrator import WallboxChargeCalibrator
from .load_profile import WeekdayLoadProfileLearner
from .solar_calibration import SolarOffsetCalibrator

_LOGGER = logging.getLogger(__name__)


def _safe_float(state) -> float | None:
    """Parse a state's numeric value, tolerating unavailable/unknown/
    missing states and non-numeric content — None rather than 0.0 on
    failure, since a wallbox SOC/target reading defaulting to 0 would
    silently invent a huge, wrong energy deficit instead of just
    skipping the computation for this cycle."""
    if state is None or state.state in ("unavailable", "unknown", ""):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


@dataclass
class DeviceState:
    """Tracks stability counters for one managed device."""
    device_id: str
    on_counter: int = 0   # consecutive cycles where ON condition was true
    off_counter: int = 0  # consecutive cycles where OFF condition was true


@dataclass
class DeviceDiagnostics:
    """Per-device values exposed to sensors, refreshed every cycle."""
    predicted_power_kw: float = 0.0
    measured_avg_kw: float | None = None
    sample_count: int = 0
    is_measured: bool = False
    is_on: bool = False
    off_only: bool = False
    dependency_met: bool = True
    off_counter: int = 0
    required_off_cycles: int = 0
    on_counter: int = 0
    required_on_cycles: int = 0
    runtime_hours_today: float = 0.0
    force_runtime: bool = False
    effective_cutoff: str | None = None
    should_be_on: bool = False
    enabled: bool = True
    priority: float = 99.0
    device_min_soc: float | None = None


@dataclass
class CoordinatorData:
    """Snapshot of all computed values, exposed to entities."""
    solar_kw: float = 0.0
    load_kw: float = 0.0
    soc: float = 0.0
    batt_kw: float = 0.0
    discharge_kw: float = 0.0
    smoothed_discharge_kw: float = 0.0
    surplus_kw: float = 0.0
    # Sum of every wallbox's dynamic reservation this cycle (see
    # _wallbox_reserved_kw) — already subtracted out of what the device
    # cascade competes over, kept here purely for visibility into why
    # devices see less than the raw surplus_kw above.
    wallbox_reserved_kw: float = 0.0
    base_load_kw: float = 0.0
    avail_kwh: float = 0.0
    # Set twice: a naive avail_kwh/discharge_rate placeholder in
    # _async_update_data, then overwritten at the end of _evaluate_devices
    # with the time-window-aware projection (see _hours_until_depleted) —
    # by the time a listener reads coordinator.data, this and batt_ok always
    # reflect the same logic the switching decisions above just used.
    h_battery: float = 999.0
    h_to_solar: float = 0.0  # raw — for display only, see effective_h_to_solar
    sun_above_horizon: bool = False
    # h_to_solar during real daytime (sun still up), capped to a short
    # fixed horizon instead of "hours until tomorrow's threshold" — this
    # is what battery-projection decisions actually use. See
    # DAYTIME_PROJECTION_HORIZON_H in const.py.
    effective_h_to_solar: float = 0.0
    solar_start: datetime | None = None
    batt_ok: bool = False
    min_soc: float = 20.0
    device_states: dict[str, bool] = field(default_factory=dict)
    device_diagnostics: dict[str, DeviceDiagnostics] = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)
    base_load_floor: dict = field(default_factory=dict)
    # Keyed by wallbox device_id — see WallboxChargeCalibrator.diagnostics.
    wallbox_max_charge: dict[str, dict] = field(default_factory=dict)
    load_profile: dict = field(default_factory=dict)
    active_solar_offset_h: float = 0.0
    next_cycle_at: datetime | None = None
    # See coordinator._battery_full_projection.
    battery_full_missing_kwh: float = 0.0
    battery_full_hours_needed: float | None = None
    battery_full_hours_until_deadline: float | None = None
    battery_full_on_track: bool = False


class PVSurplusCoordinator(DataUpdateCoordinator[CoordinatorData]):

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._config = config
        self._entry_id = entry_id
        self._device_trackers: dict[str, DeviceState] = {}
        self._power_trackers: dict[str, DevicePowerTracker] = {}
        self._runtime_trackers: dict[str, DailyRuntimeTracker] = {}
        self._discharge_samples: deque[float] = deque(maxlen=DISCHARGE_SMOOTHING_SAMPLES)
        # Same "genuinely distinct readings, not cycles" gate as
        # _last_appended_load_kw below, for _discharge_samples — the raw
        # battery charge/discharge sensor is cloud-polled on the same
        # ~5-minute cadence as the load sensor (confirmed directly against
        # real data), so this median feeds smoothed_discharge_kw (used in
        # batt_ok and the daytime base_discharge_kw branch) with the exact
        # same duplicate-inflation risk _base_load_samples had before
        # v1.27.16 fixed it — see _async_update_data.
        self._last_appended_discharge_kw: float | None = None
        # Bridges smoothed_discharge_kw across a composition-change reset
        # (see managed_on_now below), which clears _discharge_samples
        # outright. Without this, the very first sample landing in the
        # freshly-emptied deque gets ZERO smoothing (median of one value
        # IS that value) — exactly the moment attribution is most likely
        # to be transiently wrong (the load/discharge sensors haven't
        # caught up with the change yet either). Holds the last value
        # that WAS backed by >=2 genuinely independent readings until the
        # new deque earns that same trust again. None only pre-first-ever
        # reading, when there's nothing to bridge to.
        self._last_trusted_discharge_kw: float | None = None
        # Smooths base_load specifically for the overnight base_discharge_kw
        # projection — raw, unsmoothed cycle-to-cycle noise in the live load
        # reading gets multiplied by however many hours remain until solar
        # start when feeding the "would the battery last" energy total, so
        # even a small per-cycle wobble can flip a borderline device's
        # verdict back and forth all night. See _evaluate_devices.
        self._base_load_samples: deque[float] = deque(maxlen=DISCHARGE_SMOOTHING_SAMPLES)
        # Only a sample whose underlying raw load reading actually changed
        # gets admitted to _base_load_samples (see _evaluate_devices) — the
        # cloud-polled load sensor only refreshes every ~5 minutes, so
        # naively appending once per 60s coordinator cycle would fill the
        # "20-sample" window with only ~4 genuinely distinct readings, each
        # duplicated 4-5x, letting 1-2 real bad readings dominate a median
        # that looks far more robust than it actually is. None means "not
        # tracked yet / just reset" — the next reading is always admitted.
        self._last_appended_load_kw: float | None = None
        # Same composition-change bridge as _last_trusted_discharge_kw
        # above, for _base_load_samples/smoothed_base_load.
        self._last_trusted_base_load: float | None = None
        # Which devices _select_battery_optimal_set granted "would last"
        # last cycle — used to require a device that's re-joining the set
        # (was excluded last cycle, would be newly included now) to also
        # clear a more comfortable margin than the one that excluded it in
        # the first place. See RE_INCLUSION_COMFORT_BUFFER_H in const.py
        # and its use in _evaluate_devices for why this exists (a
        # razor-thin margin can flip back and forth purely from the
        # horizon shrinking as time passes, with zero change in any real
        # sensor reading).
        self._last_battery_eligible_ids: frozenset[str] = frozenset()
        # Which managed devices were on as of the last cycle — used to
        # detect a composition change and reset the discharge smoothing
        # window when one happens (see _evaluate_devices).
        self._last_managed_on: frozenset[str] = frozenset()
        # Consecutive cycles wallbox_starved has read False — gates how
        # long a device's own off_counter (which resets to 0 on any
        # single should_on cycle — see the tracker below) gets to keep
        # accumulating once the wallbox eases up. Without this, a
        # passing cloud gap lasting even one cycle would flip
        # wallbox_starved back to False, immediately re-allow
        # battery_would_last, and reset every device's off_counter right
        # as it was building toward actually switching off — on a
        # partly-cloudy day the wallbox could stay genuinely starved
        # 90% of the time and devices would still never accumulate
        # enough consecutive should_off cycles to switch, because the
        # other 10% keeps landing exactly often enough to zero the
        # counter first. See its use in _evaluate_devices. Starts fully
        # relieved, not fully starved — with no reading yet, there's no
        # evidence of starvation to protect against, so the first real
        # cycle should reflect wallbox_starved as-is rather than a
        # phantom starved period nobody observed.
        self._wallbox_relief_counter = WALLBOX_RELIEF_CYCLES
        # Tracks the managed-device mix a still-unrefreshed load/discharge
        # reading was last known to actually reflect — see the staleness
        # correction in _evaluate_devices. TWO independent freezes, not
        # one shared on both sensors: base_load's formula only ever reads
        # load_kw, and managed_discharge_kw's only ever reads discharge_kw
        # (see their respective computations) — gating BOTH on BOTH used
        # to mean base_load stayed wrongly frozen at the stale (pre-
        # change) managed power for however long discharge_kw took to
        # catch up, even once load_kw alone had already refreshed and
        # genuinely reflected the new composition. Confirmed live: a
        # device turning on (Klima) has its own multi-minute compressor
        # ramp, so its measured power lags behind the moment load_kw's
        # very next cloud-polled reading already reflects its full new
        # draw — with the old shared gate, that single fresh load_kw
        # reading got divided by a still-frozen (too-low) managed power,
        # spiking base_load by roughly the device's own wattage for
        # several minutes, exactly mirroring the composition-change
        # smoothing gap v1.27.24 fixed downstream of this. Each freeze
        # releases once its own sensor has genuinely refreshed at least
        # STALENESS_MIN_REFRESHES times since the transition (real
        # evidence it's caught up), not just after a fixed delay —
        # confirmed against real data that a fixed timer can release
        # right as the sensor is mid-refresh, before its value has
        # actually settled, capped by LOAD_SENSOR_STALENESS_GRACE so a
        # stalled sensor doesn't freeze this indefinitely.
        self._last_managed_power_kw: float = 0.0
        self._managed_power_kw_seen: bool = False
        self._stale_managed_power_kw_load: float | None = None
        self._stale_since_load: datetime | None = None
        self._last_seen_load_kw: float | None = None
        self._load_refresh_count: int = 0
        self._stale_managed_power_kw_discharge: float | None = None
        self._stale_since_discharge: datetime | None = None
        self._last_seen_discharge_kw: float | None = None
        self._discharge_refresh_count: int = 0
        # Whether each core sensor was readable as of the last cycle — a
        # system-log entry is written only on the transition (goes
        # unavailable / comes back), not every cycle it stays that way.
        self._last_sensor_valid: dict[str, bool] = {}
        # Last known good reading per core sensor, and when it first went
        # unavailable/unknown (cleared the moment it's readable again) —
        # see _get_core_float and CORE_SENSOR_GRACE_PERIOD.
        self._core_sensor_last_good: dict[str, float] = {}
        self._core_sensor_invalid_since: dict[str, datetime] = {}
        # The should_be_on target last pushed to the native HA logbook per
        # device — the log table (self.log_entries) gets every cycle
        # unconditionally, but the native logbook (and anything built on
        # it, like the Diagnose tab's Schaltvorgänge column) should only
        # ever show real transitions, not a repeated "still on" every
        # cycle.
        self._last_should_be_on: dict[str, bool] = {}
        # Consecutive cycles a wallbox's own power draw has been below the
        # idle threshold — see _wallbox_satisfied's idle-release check.
        self._wallbox_idle_counters: dict[str, int] = {}
        # Serializes every read-modify-write of the config entry's data
        # (device enabled/priority/power/etc. number & select entities, the
        # per-device enabled switch) — without this, toggling several of
        # these entities at once (e.g. a "toggle all" dashboard button) is a
        # real race: each one reads entry.data before any of the others has
        # written back, so only the last write to land actually sticks and
        # the rest are silently lost. Confirmed happening in practice.
        self.config_write_lock = asyncio.Lock()
        # Rolling table of every logged event (device decisions every
        # cycle, plus system-level events like recalibration), newest
        # first — exposed as a sensor attribute so the dashboard can render
        # a real Datum/Gerät/Titel/Details table instead of the fixed
        # timeline layout of a logbook card. Capped well under the
        # recorder's attribute-size warning threshold; this entity's own
        # state history isn't meaningful to record anyway, only its current
        # (live-templated) attribute value is.
        self.log_entries: deque[dict] = deque(maxlen=1000)
        self._calibrator = SolarOffsetCalibrator(hass, entry_id, config[CONF_SOLAR_SENSOR])
        self._base_load_floor_calibrator = BaseLoadFloorCalibrator(
            hass, entry_id, config[CONF_LOAD_SENSOR]
        )
        # One calibrator per wallbox with a power sensor — capacity/SOC
        # config and charging history are per-car, so this can't be a
        # single shared instance the way the two calibrators above are.
        self._wallbox_charge_calibrators: dict[str, WallboxChargeCalibrator] = {}
        for dev in config.get(CONF_DEVICES, []):
            if not dev.get(CONF_DEVICE_IS_WALLBOX, False):
                continue
            power_sensor = dev.get(CONF_DEVICE_POWER_SENSOR)
            if not power_sensor:
                continue
            self._wallbox_charge_calibrators[dev["_id"]] = WallboxChargeCalibrator(
                hass, entry_id, dev["_id"], power_sensor
            )
        self._load_profile_learner = WeekdayLoadProfileLearner(hass, entry_id)
        self._last_offset_h = 0.0
        for dev in config.get(CONF_DEVICES, []):
            device_id = dev["_id"]
            self._device_trackers[device_id] = DeviceState(device_id=device_id)

    async def async_setup_power_trackers(self) -> None:
        """Load persisted per-device state: power samples (only if a power
        sensor is configured) and today's accumulated runtime (always, so
        the minimum daily runtime feature has history even if it's enabled
        later). Also loads the last computed solar-offset calibration."""
        await self._calibrator.async_load()
        await self._base_load_floor_calibrator.async_load()
        for calibrator in self._wallbox_charge_calibrators.values():
            await calibrator.async_load()
        await self._load_profile_learner.async_load()
        for dev in self._config.get(CONF_DEVICES, []):
            if dev.get(CONF_DEVICE_IS_WALLBOX, False):
                continue
            device_id = dev["_id"]

            sensor_id = dev.get(CONF_DEVICE_POWER_SENSOR)
            if sensor_id:
                power_tracker = DevicePowerTracker(self.hass, self._entry_id, device_id)
                await power_tracker.async_load()
                self._power_trackers[device_id] = power_tracker

            runtime_tracker = DailyRuntimeTracker(self.hass, self._entry_id, device_id)
            await runtime_tracker.async_load()
            self._runtime_trackers[device_id] = runtime_tracker

    async def async_flush_stores(self) -> None:
        """Force-write every debounced Store immediately — runtime
        trackers and power trackers re-trigger their own debounce timer
        on every single coordinator cycle while active, which can leave
        them without a quiet window to actually flush to disk on their
        own for an entire day (see DailyRuntimeTracker.async_save_now).
        Call this right before the integration unloads — every version
        update reloads it — so today's tracking survives instead of
        reverting to a stale disk copy on the next load.
        """
        for tracker in self._runtime_trackers.values():
            await tracker.async_save_now()
        for tracker in self._power_trackers.values():
            await tracker.async_save_now()
        await self._load_profile_learner.async_save_now()

    @property
    def devices(self) -> list[dict]:
        return sorted(
            self._config.get(CONF_DEVICES, []),
            key=lambda d: d.get(CONF_DEVICE_PRIORITY, 99),
        )

    def _get_core_float(self, entity_id: str) -> float:
        """Read one of the four core sensors (solar/load/soc/battery),
        tolerating a brief unavailable/unknown blip by holding its last
        known good value for up to CORE_SENSOR_GRACE_PERIOD before giving
        up and raising UpdateFailed.

        Without any of this, a sensor going "unknown" (e.g. a brief
        integration hiccup) would silently read as 0 — 0 solar looks
        exactly like "no sun" to the cascade, and after the off-hold
        buffer expires, devices would actually be switched off because of
        a communication glitch, not a real drop in production. The
        previous version of this check raised UpdateFailed immediately on
        any invalid reading — safe, but on a real installation most
        FusionSolarPlus blips clear within ~10-25 minutes, and freezing
        the whole coordinator (skipping every device's evaluation) for
        each one turned out to be far more disruptive than briefly
        computing off a reading that's a few minutes stale. A genuinely
        extended outage (observed once: ~5 hours) still needs to freeze
        rather than run forever on an increasingly stale number — that's
        what the grace period boundary is for.
        """
        state = self.hass.states.get(entity_id)
        value: float | None = None
        if state is not None and state.state not in ("unavailable", "unknown", ""):
            try:
                value = float(state.state)
            except ValueError:
                value = None
        valid = value is not None
        now = dt_util.utcnow()

        # Only log real transitions — the entity's first-ever observation
        # (right after startup/reload) always looks like a "change" against
        # the empty dict otherwise, which would falsely announce "wieder
        # verfügbar" for every core sensor on every single restart even
        # though nothing was ever actually down.
        was_tracked = entity_id in self._last_sensor_valid
        if was_tracked and self._last_sensor_valid[entity_id] != valid:
            titel = "Sensor wieder verfügbar" if valid else "Sensor nicht verfügbar"
            details = (
                f"{entity_id} wieder verfügbar" if valid
                else (
                    f"{entity_id} nicht verfügbar — verwende letzten bekannten Wert für bis "
                    f"zu {int(CORE_SENSOR_GRACE_PERIOD.total_seconds() // 60)} Min., danach "
                    "wird der Zyklus übersprungen"
                )
            )
            self.hass.async_create_task(self._log_system(titel, details))
        self._last_sensor_valid[entity_id] = valid

        if valid:
            self._core_sensor_last_good[entity_id] = value
            self._core_sensor_invalid_since.pop(entity_id, None)
            return value

        if entity_id not in self._core_sensor_invalid_since:
            self._core_sensor_invalid_since[entity_id] = now
        invalid_for = now - self._core_sensor_invalid_since[entity_id]
        if invalid_for <= CORE_SENSOR_GRACE_PERIOD and entity_id in self._core_sensor_last_good:
            return self._core_sensor_last_good[entity_id]

        raise UpdateFailed(f"{entity_id} is unavailable/unknown — skipping this cycle")

    def _get_power_kw(self, entity_id: str | None) -> float:
        """Read a power sensor, normalising W to kW."""
        if not entity_id:
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return 0.0
        try:
            value = float(state.state)
        except ValueError:
            return 0.0
        unit = (state.attributes.get("unit_of_measurement") or "kW").upper()
        return value / 1000.0 if unit == "W" else value

    def _record(self, wer: str, titel: str, details: str) -> None:
        """Append one row to the rolling log table (newest first) that the
        dashboard's Logs tab renders as an actual Datum/Gerät/Titel/Details
        table — this is separate from the logbook.log calls below, which
        feed Home Assistant's own native Logbook."""
        self.log_entries.appendleft({
            "zeit": dt_util.now().isoformat(),
            "wer": wer,
            "titel": titel,
            "details": details,
        })

    async def _log_decision(self, dev: dict, should_be_on: bool, titel: str, details: str) -> None:
        """Record a device's cascade decision this cycle — every cycle, not
        just when the target changes, into the log table (self.log_entries,
        what the Logs tab's table reads) since the user wants to see every
        calculation there. The native HA logbook is different: it only gets
        an entry when should_be_on actually flips, since anything built on
        top of the logbook (the Diagnose tab's Schaltvorgänge column, HA's
        own Logbook page) is meant to show real transitions, not a repeated
        "still on" every cycle. Attributed to the device's own managed
        switch, not its power sensor — Home Assistant's logbook UI silently
        drops logbook.log entries for the "sensor" domain, so attributing
        this to a sensor entity meant the entry existed via the API but
        never rendered anywhere in the frontend."""
        device_id = dev["_id"]
        name = dev.get(CONF_DEVICE_NAME, device_id)
        self._record(name, titel, details)

        changed = self._last_should_be_on.get(device_id) != should_be_on
        self._last_should_be_on[device_id] = should_be_on
        if not changed:
            return

        entity_id = er.async_get(self.hass).async_get_entity_id(
            "switch", DOMAIN, f"{self._entry_id}_{device_id}_managed"
        )
        service_data = {"name": name, "message": f"{titel} — {details}", "domain": DOMAIN}
        if entity_id:
            service_data["entity_id"] = entity_id
        await self.hass.services.async_call("logbook", "log", service_data, blocking=False)

    async def _log_system(self, titel: str, details: str) -> None:
        """Write a logbook + log-table entry for something background/
        system-level (not a per-device decision) — a recalibration
        finishing, a cycle being skipped because a core sensor went
        unavailable, etc. Attributed to the always-on system binary_sensor,
        since a plain "sensor" domain entry never renders in the logbook
        UI."""
        self._record("SLS", titel, details)
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{self._entry_id}_system_status"
        )
        service_data = {"name": "System", "message": f"{titel} — {details}", "domain": DOMAIN}
        if entity_id:
            service_data["entity_id"] = entity_id
        await self.hass.services.async_call("logbook", "log", service_data, blocking=False)

    def _predicted_power_kw(self, dev: dict) -> tuple[float, DeviceDiagnostics]:
        """Return (predicted_power_kw, diagnostics) — measured average if enough
        samples exist, otherwise the configured estimate."""
        tracker = self._power_trackers.get(dev["_id"])
        configured = dev.get(CONF_DEVICE_POWER_KW, 0.15)

        diag = DeviceDiagnostics()
        if tracker is not None:
            diag.measured_avg_kw = tracker.average_kw
            diag.sample_count = tracker.sample_count

        if tracker is not None and tracker.sample_count >= MIN_SAMPLES_FOR_MEASURED_AVG:
            avg = tracker.average_kw
            if avg is not None and avg > 0:
                diag.is_measured = True
                diag.predicted_power_kw = avg
                return avg, diag

        diag.predicted_power_kw = configured
        return configured, diag

    def _wallbox_satisfied(self, wallbox_dev: dict) -> bool:
        """Whether a device depending on this wallbox may run. A wallbox is
        never itself switched by the cascade, so "is it on" doesn't apply
        to it the way it does for a normal dependency — this checks its
        own charging power instead, two ways:

        - "Satisfied": its own power draw has already reached the
          configured threshold, so it's getting plenty and a dependent
          device isn't meaningfully taking anything away from it.
        - "Idle": its power draw has been below a low threshold for the
          same hold time every other decision uses — sustained near-zero
          draw looks the same whether the car's unplugged/gone or it's
          simply finished charging, and either way there's no reason left
          to keep holding a dependent device back for it. This needs no
          configuration beyond the wallbox's own required power_sensor,
          unlike trying to compare SOC against a target (which can't tell
          "car not here" from "car still charging" apart at all, since a
          departed car keeps reporting whatever SOC it had).
        """
        satisfied_kw = wallbox_dev.get(CONF_WALLBOX_SATISFIED_KW)
        wallbox_id = wallbox_dev["_id"]
        power_sensor = wallbox_dev.get(CONF_DEVICE_POWER_SENSOR)
        wallbox_power_kw = self._get_power_kw(power_sensor)

        if satisfied_kw and wallbox_power_kw >= satisfied_kw:
            self._wallbox_idle_counters[wallbox_id] = 0
            return True

        if wallbox_power_kw < WALLBOX_IDLE_THRESHOLD_KW:
            counter = self._wallbox_idle_counters.get(wallbox_id, 0) + 1
            self._wallbox_idle_counters[wallbox_id] = counter
            if counter >= STABLE_ON_CYCLES:
                return True
        else:
            self._wallbox_idle_counters[wallbox_id] = 0

        return False

    def _in_window(self, dev: dict) -> bool | None:
        """True/False if this device is restricted to a schedule, else None
        (no restriction = always eligible).

        A schedule.* helper entity takes priority when configured — Home
        Assistant's own schedule helper natively supports multiple blocks
        per day and per-weekday configuration, which a single start/end
        pair can't represent. Falls back to a simple daily start/end window
        (supports wrapping past midnight, e.g. 22:00-06:00) if no helper is
        set.
        """
        schedule_entity = dev.get(CONF_DEVICE_SCHEDULE_ENTITY)
        if schedule_entity:
            state = self.hass.states.get(schedule_entity)
            if state is None or state.state in ("unavailable", "unknown"):
                # Helper broken/not yet loaded — fail open rather than
                # force the device off on every restart.
                return None
            return state.state == "on"

        start_str = dev.get(CONF_DEVICE_WINDOW_START)
        end_str = dev.get(CONF_DEVICE_WINDOW_END)
        if not start_str or not end_str:
            return None

        now_t = dt_util.now().time()
        start_t = dt_util.parse_time(start_str)
        end_t = dt_util.parse_time(end_str)
        if start_t is None or end_t is None:
            return None

        if start_t <= end_t:
            return start_t <= now_t < end_t
        return now_t >= start_t or now_t < end_t  # wraps past midnight

    def _window_reopens_within(self, dev: dict, now: datetime, horizon: timedelta) -> bool:
        """True if a currently-closed window's next start is within `horizon`.

        Only called for devices already known to be window/schedule
        restricted (in_window is False, i.e. _in_window returned a real
        schedule/window, just not open right now) — used to tell "about to
        open" apart from "just closed for the day, next open is tomorrow".
        Without this, pre-charging (see the per-device loop) can't
        distinguish the two: it would prime on_counter and show a "wartet
        noch X min bis einschalten" countdown for hours after a window
        closes, implying an imminent switch-on that's actually many hours
        away.
        """
        schedule_entity = dev.get(CONF_DEVICE_SCHEDULE_ENTITY)
        if schedule_entity:
            state = self.hass.states.get(schedule_entity)
            if state is None or state.state in ("unavailable", "unknown"):
                return False
            # While the schedule is "off", its own next_event is the next
            # moment it turns "on" — see _effective_cutoff above for the
            # datetime-vs-string handling this mirrors.
            next_event_raw = state.attributes.get("next_event")
            if isinstance(next_event_raw, datetime):
                next_event = next_event_raw
            elif isinstance(next_event_raw, str):
                next_event = dt_util.parse_datetime(next_event_raw)
            else:
                return False
            if next_event is None:
                return False
            return dt_util.as_utc(next_event) - now <= horizon

        start_str = dev.get(CONF_DEVICE_WINDOW_START)
        start_t = dt_util.parse_time(start_str) if start_str else None
        if start_t is None:
            return False
        candidate = dt_util.now().replace(
            hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
        )
        candidate_utc = dt_util.as_utc(candidate)
        if candidate_utc <= now:
            candidate_utc += timedelta(days=1)
        return candidate_utc - now <= horizon

    def _own_window_end(self, dev: dict, now: datetime) -> datetime | None:
        """This device's own configured window close — a schedule.*
        helper's next "off" event while currently "on", or a plain
        window_end time (next occurrence, including past-midnight
        wraparound). None if the device has no configured window at all.

        Deliberately narrower than _effective_cutoff below: excludes the
        stops_overnight and dependency-inherited fallbacks, which are
        synthetic/derived and not a real window boundary — using them
        here would make minimum-daily-runtime forcing (see
        _force_runtime_active) fire on a rolling, ever-recomputed
        deadline instead of an actual fixed close time.
        """
        schedule_entity = dev.get(CONF_DEVICE_SCHEDULE_ENTITY)
        if schedule_entity:
            state = self.hass.states.get(schedule_entity)
            if state is not None and state.state == "on":
                next_event_raw = state.attributes.get("next_event")
                if isinstance(next_event_raw, datetime):
                    next_event = next_event_raw
                elif isinstance(next_event_raw, str):
                    next_event = dt_util.parse_datetime(next_event_raw)
                else:
                    next_event = None
                if next_event is not None:
                    return dt_util.as_utc(next_event)
            return None

        window_end_str = dev.get(CONF_DEVICE_WINDOW_END)
        end_t = dt_util.parse_time(window_end_str) if window_end_str else None
        if end_t is None:
            return None
        candidate = dt_util.now().replace(
            hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0
        )
        candidate_utc = dt_util.as_utc(candidate)
        if candidate_utc <= now:
            candidate_utc += timedelta(days=1)
        return candidate_utc

    def _solar_noon_passed(self) -> bool:
        """Whether today's actual solar peak (Sonnenhöchstand) has
        already occurred — the windowless-device fallback trigger for
        minimum-daily-runtime forcing, in place of a fixed clock hour.
        Solar noon (not clock noon) is the meaningful "half of today's
        sunlight is behind us" marker for a PV-surplus system, and shifts
        with season/DST/longitude the same way solar-start calibration
        already accounts for elsewhere in this file. Falls back to the
        fixed MIN_RUNTIME_FORCE_AFTER_HOUR clock hour if sun.sun or its
        next_noon attribute isn't available.
        """
        sun = self.hass.states.get("sun.sun")
        next_noon_raw = sun.attributes.get("next_noon") if sun is not None else None
        if isinstance(next_noon_raw, datetime):
            next_noon = next_noon_raw
        elif isinstance(next_noon_raw, str):
            next_noon = dt_util.parse_datetime(next_noon_raw)
        else:
            next_noon = None
        if next_noon is None:
            return dt_util.now().hour >= MIN_RUNTIME_FORCE_AFTER_HOUR

        local_now = dt_util.now()
        local_next_noon = dt_util.as_local(next_noon)
        solar_noon_today = (
            local_next_noon
            if local_next_noon.date() == local_now.date()
            else local_next_noon - timedelta(hours=24)
        )
        return local_now >= solar_noon_today

    def _force_runtime_active(
        self, dev: dict, now: datetime, runtime_hours_today: float, predicted_power_kw: float
    ) -> bool:
        """Whether the minimum-daily-runtime target must be forced on
        right now, regardless of surplus/battery.

        Two independent triggers, whichever fires first:

        1. Forecast-based, early: if CONF_SOLAR_FORECAST_REMAINING_ENTITY
           is configured, and this device's own remaining energy need
           (missing hours × its predicted power) already amounts to more
           than FORCE_RUNTIME_FORECAST_SHARE_MAX of *all* solar the
           forecast still expects today, the day's unlikely to deliver
           enough free surplus for this device alongside everything else
           the house needs — force now rather than wait. This is what
           lets a device start catching up around midday on a visibly
           weak day, instead of only in the evening once it's too late
           to do anything but draw straight off the battery. Confirmed
           live: waiting for the deadline trigger below meant Pool-Pumpe
           only started forcing at dusk on a weak day, drawing ~2.5 kWh
           straight from an already under-charged battery right before
           an already-tight night.

        2. Deadline-based, the original fallback: a device with a real
           window (schedule.* or window_end) is forced once the time
           remaining until that window closes — plus
           MIN_RUNTIME_FORCE_BUFFER_H of safety margin — is no longer
           enough to freely reach the still-missing hours on its own,
           guaranteeing the target lands before the window shuts rather
           than after. Windowless devices fall back to the fixed
           solar-noon trigger instead, since there's no window end to
           measure against. Always active regardless of the forecast
           trigger above (e.g. no forecast entity configured, or the
           forecast simply hasn't flagged trouble yet) — the last-resort
           guarantee that the target still gets hit one way or another.
        """
        min_daily_runtime_h = dev.get(CONF_DEVICE_MIN_DAILY_RUNTIME_H)
        if min_daily_runtime_h is None or runtime_hours_today >= min_daily_runtime_h:
            return False
        missing_h = min_daily_runtime_h - runtime_hours_today

        forecast_entity = self._config.get(CONF_SOLAR_FORECAST_REMAINING_ENTITY)
        forecast_remaining_kwh = (
            _safe_float(self.hass.states.get(forecast_entity)) if forecast_entity else None
        )
        if forecast_remaining_kwh is not None:
            missing_kwh = missing_h * predicted_power_kw
            if missing_kwh >= forecast_remaining_kwh * FORCE_RUNTIME_FORECAST_SHARE_MAX:
                return True

        own_window_end = self._own_window_end(dev, now)
        if own_window_end is not None:
            remaining_h = (own_window_end - now).total_seconds() / 3600.0
            return remaining_h <= missing_h + MIN_RUNTIME_FORCE_BUFFER_H

        return self._solar_noon_passed()

    def _wallbox_reservation_rate(
        self, missing_kwh: float, now: datetime, available_surplus: float
    ) -> float | None:
        """The raw (pre-cap) reservation rate in kW, before
        _wallbox_reserved_kw applies the surplus/max-charge caps. None
        means "can't compute right now" (missing sun.sun data), not "no
        reservation" — the caller distinguishes.

        Prefers a forecast-based proportional rate when
        CONF_SOLAR_FORECAST_REMAINING_ENTITY is configured: claim the
        same *share* of whatever solar is happening right now as the
        share of today's still-forecast solar the deficit represents
        (missing_kwh / forecast_kwh_still_to_come). A flat clock-time
        average treats a weak 9am and a strong 2pm identically, which is
        exactly backwards — confirmed live: reserving a flat rate from
        the morning onward claimed real surplus during hours when actual
        production was still ramping up, while a large deficit
        discovered at midday would need an even larger flat rate for the
        rest of the day than the true solar curve could comfortably
        supply. The forecast-based rate instead scales naturally with
        the sun itself: low at 9am when little is forecast to still
        arrive relative to the whole day, higher once the afternoon peak
        is actually forecast to deliver it.

        Falls back to the flat sunset-minus-buffer/hours-remaining
        formula when no forecast entity is configured (or its reading is
        unavailable) — worse-shaped, but still deadline-aware and still
        fully capped downstream, so the feature keeps working without
        Forecast.Solar or an equivalent integration installed.
        """
        forecast_entity = self._config.get(CONF_SOLAR_FORECAST_REMAINING_ENTITY)
        forecast_remaining_kwh = (
            _safe_float(self.hass.states.get(forecast_entity)) if forecast_entity else None
        )
        if forecast_remaining_kwh is not None:
            share_needed = missing_kwh / max(forecast_remaining_kwh, WALLBOX_FORECAST_MIN_KWH)
            return share_needed * max(available_surplus, 0.0)

        sun = self.hass.states.get("sun.sun")
        next_setting_raw = sun.attributes.get("next_setting") if sun is not None else None
        if isinstance(next_setting_raw, datetime):
            next_setting = next_setting_raw
        elif isinstance(next_setting_raw, str):
            next_setting = dt_util.parse_datetime(next_setting_raw)
        else:
            next_setting = None
        if next_setting is None:
            return None

        deadline = next_setting - timedelta(hours=WALLBOX_TARGET_TIME_BUFFER_H)
        hours_remaining = max(
            (deadline - now).total_seconds() / 3600.0, WALLBOX_TARGET_MIN_HOURS
        )
        return missing_kwh / hours_remaining

    def _wallbox_reserved_kw(
        self, wallbox_dev: dict, now: datetime, available_surplus: float
    ) -> float:
        """How much of the current surplus (kW) to set aside for this
        wallbox before the device cascade gets to compete for the rest.
        Computed dynamically from how many kWh are
        still missing to reach the charge target and how many hours are
        realistically left to get there (sunset minus a safety buffer),
        so it eases off on its own as the car approaches its target or
        the deadline gets closer, instead of a flat number that's either
        too little on a good day or too much on a weak one.

        No fixed "don't start before X" gate — an earlier version waited
        for solar noon, which is wrong for a large deficit: at, say, 60%
        still missing and an 11 kW charger, even a full afternoon at max
        power might not be enough, so waiting for it to even start is
        exactly backwards. The formula is self-limiting without a gate:
        capped at the surplus that actually exists right now (can't
        manufacture morning surplus that isn't there — the house battery
        and everything else naturally still gets whatever the wallbox
        doesn't need or can't use yet) AND at the wallbox's own maximum
        charge rate (no point reserving more than the car could ever
        draw). Whatever's left over after both caps is exactly what's
        available for the wallbox to actually use — nothing is being
        held back from other devices for a share the wallbox couldn't
        draw anyway.
        """
        capacity_entity = wallbox_dev.get(CONF_WALLBOX_CAPACITY_ENTITY)
        soc_entity = wallbox_dev.get(CONF_WALLBOX_SOC_ENTITY)
        target_entity = wallbox_dev.get(CONF_WALLBOX_TARGET_SOC_ENTITY)
        if not capacity_entity or not soc_entity or not target_entity:
            return 0.0

        capacity_kwh = _safe_float(self.hass.states.get(capacity_entity))
        current_soc = _safe_float(self.hass.states.get(soc_entity))
        target_soc = _safe_float(self.hass.states.get(target_entity))
        if capacity_kwh is None or current_soc is None or target_soc is None:
            return 0.0

        missing_kwh = max(capacity_kwh * (target_soc - current_soc) / 100.0, 0.0)
        if missing_kwh <= 0:
            return 0.0

        # The SOC/target-SOC entities only ever hold the car's last known
        # reading — if it's away, that's whatever it was when it left,
        # not "0% missing". Without a presence check there'd be no way to
        # tell "genuinely needs to charge" apart from "not here to charge
        # at all" — confirmed live: SOC below target while away held back
        # real surplus for hours with nothing to actually use it.
        present_entity = wallbox_dev.get(CONF_WALLBOX_PRESENT_ENTITY)
        if present_entity:
            present_state = self.hass.states.get(present_entity)
            # "Present" is an allowlist ("on" for a binary_sensor, "home"
            # for a device_tracker/person), not a denylist of specific
            # away-values — a device_tracker's state is the name of
            # whichever zone it's currently in, and a *named* zone other
            # than home (e.g. "Arbeit") is just as much "not here" as the
            # generic "not_home" state is, but wasn't being recognized as
            # such. Confirmed live: the reservation kept claiming surplus
            # for hours after the car left for a zone called "Arbeit".
            if present_state is None or present_state.state not in ("on", "home"):
                return 0.0

        reserved_kw = self._wallbox_reservation_rate(missing_kwh, now, available_surplus)
        if reserved_kw is None:
            return 0.0

        # A manually-entered cap always wins if set; otherwise fall back
        # to the learned rolling max from this wallbox's own charging
        # history (see wallbox_charge_calibrator.py) — self-adjusting to
        # whatever's actually achievable this month instead of a number
        # that silently goes stale the moment reality changes.
        max_charge_kw = wallbox_dev.get(CONF_WALLBOX_MAX_CHARGE_KW)
        if not max_charge_kw:
            calibrator = self._wallbox_charge_calibrators.get(wallbox_dev.get("_id"))
            max_charge_kw = calibrator.max_charge_kw if calibrator else None
        if max_charge_kw:
            reserved_kw = min(reserved_kw, max_charge_kw)
        return min(reserved_kw, max(available_surplus, 0.0))

    def _battery_full_projection(
        self, soc: float, batt: float, battery_kwh: float, now: datetime
    ) -> tuple[float, float | None, float | None, bool]:
        """Whether the house battery is on track to reach
        WEAK_DAY_BATTERY_FULL_SOC by sunset minus a safety margin,
        projected from the *current* live charge rate — a rough,
        live-snapshot estimate (no smoothing of its own, unlike the
        discharge-rate projection elsewhere in this file), since this is
        diagnostic-only and not itself a switching input. Confirmed
        useful live: on a "weak day" that's nonetheless charging
        strongly right now, a five-minute mental calculation (missing
        kWh ÷ current charge rate vs. hours left) already answers "will
        it make it" far more usefully than the weak-day flag alone,
        which only looks backward at today's gain so far, never forward.

        Returns (missing_kwh, hours_needed, hours_until_deadline,
        on_track). hours_needed is None while the battery isn't
        genuinely charging right now (can't project a rate of ~0
        forward — that's "stalled", not "will take forever" the way a
        naive division would suggest). on_track is True outright once
        missing_kwh is already ~0.
        """
        missing_kwh = max(battery_kwh * (WEAK_DAY_BATTERY_FULL_SOC - soc) / 100.0, 0.0)
        if missing_kwh <= 0:
            return 0.0, 0.0, None, True

        charge_kw = max(batt, 0.0)
        hours_needed = missing_kwh / charge_kw if charge_kw > 0.05 else None

        sun = self.hass.states.get("sun.sun")
        next_setting_raw = sun.attributes.get("next_setting") if sun is not None else None
        if isinstance(next_setting_raw, datetime):
            next_setting = next_setting_raw
        elif isinstance(next_setting_raw, str):
            next_setting = dt_util.parse_datetime(next_setting_raw)
        else:
            next_setting = None
        if next_setting is None:
            return missing_kwh, hours_needed, None, False

        deadline = next_setting - timedelta(hours=BATTERY_FULL_TARGET_TIME_BUFFER_H)
        hours_until_deadline = max((deadline - now).total_seconds() / 3600.0, 0.0)
        on_track = hours_needed is not None and hours_needed <= hours_until_deadline
        return missing_kwh, hours_needed, hours_until_deadline, on_track

    def _effective_cutoff(
        self,
        dev: dict,
        now: datetime,
        devices_by_id: dict[str, dict],
        _visited: frozenset[str] | None = None,
    ) -> datetime | None:
        """The next known moment this device's power draw will drop to
        zero, or None if there's no way to know (it might keep drawing all
        the way to the projection horizon).

        Four independent sources, the earliest of which wins since any one
        of them alone forces the device off:
        - A schedule.* helper's own `next_event` attribute while the
          schedule is currently "on" — next_event is then necessarily the
          moment it turns off.
        - A simple window_end time (next occurrence from now, including
          past-midnight wraparound).
        - stops_overnight, only when neither of the two above applies at
          all (not just "not currently active" — a device with a real but
          currently-off window still has none of the above contribute a
          candidate, and shouldn't fall back to this either). A rolling
          "at most DEFAULT_MAX_ASSUMED_RUNTIME_H hours from right now"
          instead of a fixed clock time, re-derived every call — see
          const.py's CONF_DEVICE_STOPS_OVERNIGHT for why this exists.
        - Inherited from a prerequisite device's own cutoff, if this
          device depends on one — it gets forced off the instant its
          prerequisite does, regardless of its own window/schedule.
        """
        _visited = _visited or frozenset()
        device_id = dev.get("_id")
        if device_id in _visited:
            return None  # guards against a misconfigured dependency cycle
        _visited = _visited | {device_id}

        candidates: list[datetime] = []
        has_configured_window = False

        schedule_entity = dev.get(CONF_DEVICE_SCHEDULE_ENTITY)
        if schedule_entity:
            has_configured_window = True
            state = self.hass.states.get(schedule_entity)
            if state is not None and state.state == "on":
                # The schedule integration stores next_event as a native
                # datetime object on the in-memory State (unlike the REST/
                # websocket APIs, which JSON-serialize it to a string) —
                # accept either rather than assuming one.
                next_event_raw = state.attributes.get("next_event")
                if isinstance(next_event_raw, datetime):
                    next_event = next_event_raw
                elif isinstance(next_event_raw, str):
                    next_event = dt_util.parse_datetime(next_event_raw)
                else:
                    next_event = None
                if next_event is not None:
                    candidates.append(dt_util.as_utc(next_event))
        else:
            window_end_str = dev.get(CONF_DEVICE_WINDOW_END)
            end_t = dt_util.parse_time(window_end_str) if window_end_str else None
            if end_t is not None:
                has_configured_window = True
                candidate = dt_util.now().replace(
                    hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0
                )
                candidate_utc = dt_util.as_utc(candidate)
                if candidate_utc <= now:
                    candidate_utc += timedelta(days=1)
                candidates.append(candidate_utc)

        if not has_configured_window and dev.get(CONF_DEVICE_STOPS_OVERNIGHT, False):
            candidates.append(now + timedelta(hours=DEFAULT_MAX_ASSUMED_RUNTIME_H))

        depends_on_id = dev.get(CONF_DEVICE_DEPENDS_ON)
        if depends_on_id:
            prereq = devices_by_id.get(depends_on_id)
            if prereq is not None:
                prereq_cutoff = self._effective_cutoff(prereq, now, devices_by_id, _visited)
                if prereq_cutoff is not None:
                    candidates.append(prereq_cutoff)

        return min(candidates) if candidates else None

    @staticmethod
    def _project_energy_kwh(
        segments: list[tuple[float, datetime | None]],
        now: datetime,
        horizon_end: datetime,
        base_discharge_kw: float,
        available_surplus: float,
    ) -> float:
        """Projected battery energy (kWh) drawn between now and
        horizon_end, given a set of committed devices that each draw
        constant power until their own known cutoff (or indefinitely, if
        cutoff is None).

        This is what makes the overnight projection aware of time windows
        and schedules: a device with a known cutoff drops out of the load
        at that point instead of being assumed to keep drawing all the way
        to horizon_end, which would otherwise make lower-priority devices'
        projections needlessly pessimistic once a higher-priority
        windowed device is due to stop anyway. With no cutoffs at all this
        reduces to exactly the old single constant-rate calculation.
        """
        if horizon_end <= now:
            return 0.0

        boundaries = sorted({c for _, c in segments if c is not None and now < c < horizon_end})
        boundaries = [now, *boundaries, horizon_end]

        energy = 0.0
        for seg_start, seg_end in zip(boundaries, boundaries[1:]):
            seg_hours = (seg_end - seg_start).total_seconds() / 3600.0
            if seg_hours <= 0:
                continue
            active_power = sum(p for p, c in segments if c is None or c > seg_start)
            # available_surplus must be floored at 0 here — a negative
            # surplus means the base load alone already exceeds solar,
            # which is exactly what base_discharge_kw accounts for
            # separately. Without the floor, a negative surplus would add
            # its own magnitude on top of active_power instead of just
            # failing to cover it, double-counting the base load's deficit
            # once through base_discharge_kw and again here.
            uncovered = max(active_power - max(available_surplus, 0.0), 0.0)
            energy += (base_discharge_kw + uncovered) * seg_hours
        return energy

    @staticmethod
    def _hours_until_depleted(
        segments: list[tuple[float, datetime | None]],
        now: datetime,
        avail_kwh: float,
        base_discharge_kw: float,
        available_surplus: float,
        max_horizon_h: float = 999.0,
    ) -> float:
        """Hours from now until the projected energy use would exceed
        avail_kwh — the exact inverse of _project_energy_kwh, walking the
        same time-windowed segments forward instead of a fixed horizon.

        This is what the "Akku reicht" diagnostic uses instead of a flat
        avail_kwh / current_discharge_rate division: that division assumes
        today's discharge rate holds constant all night, which looks like a
        shortfall the moment a device with a known cutoff (a time window or
        schedule) is part of the current draw, even though it's about to
        drop out and free up that headroom. Walking the same segments the
        real should_on/should_off decision is based on keeps this number
        honest about what the cascade actually expects to happen.
        """
        horizon_end = now + timedelta(hours=max_horizon_h)
        boundaries = sorted({c for _, c in segments if c is not None and now < c < horizon_end})
        boundaries = [now, *boundaries, horizon_end]

        remaining_kwh = avail_kwh
        for seg_start, seg_end in zip(boundaries, boundaries[1:]):
            seg_hours = (seg_end - seg_start).total_seconds() / 3600.0
            if seg_hours <= 0:
                continue
            active_power = sum(p for p, c in segments if c is None or c > seg_start)
            uncovered = max(active_power - max(available_surplus, 0.0), 0.0)
            rate = base_discharge_kw + uncovered
            if rate <= 0:
                continue  # this segment doesn't drain the battery at all
            seg_energy = rate * seg_hours
            if seg_energy >= remaining_kwh:
                hours_into_segment = remaining_kwh / rate
                return (seg_start - now).total_seconds() / 3600.0 + hours_into_segment
            remaining_kwh -= seg_energy
        return max_horizon_h

    def _select_battery_optimal_set(
        self,
        candidates: list[tuple[str, float, datetime | None, int]],
        mandatory_segments: list[tuple[float, datetime | None]],
        now: datetime,
        horizon_end: datetime,
        base_discharge_kw: float,
        available_surplus: float,
        avail_kwh: float,
        soc: float,
        min_soc: float,
        max_priority_number: int,
    ) -> frozenset[str]:
        """Which of `candidates` (device_id, predicted_power, own_cutoff,
        priority) should count as "battery would last" this cycle.

        The old per-device check was purely sequential: each device only
        saw what higher-priority devices already committed, then checked
        whether its OWN addition (with its own cutoff) still fit. That's
        fine for the *surplus* check (a live, moment-to-moment thing,
        still handled sequentially elsewhere) but produces a real paradox
        for the *overnight battery* check: a lower-priority device with a
        known cutoff (bounded total energy) can pass while a *higher*-
        priority device with no cutoff (assumed to draw power all the way
        to solar start, since there's no known stopping point) fails —
        even though shedding the lower-priority device wouldn't have
        helped the higher-priority one at all, since it already saw the
        full, uncommitted margin when it was evaluated first, before
        anyone else had a chance to claim any of it.

        Instead: find the combination of candidates that fits within
        avail_kwh together with the always-on mandatory_segments (disabled-
        but-physically-running devices, and force-runtime devices — both
        already committed regardless of what this picks) while keeping the
        most priority-weighted value. This sheds only what's actually
        necessary rather than a strict lowest-priority-first order: a
        cheap, low-priority device that wouldn't free up enough to matter
        stays on, while a pricier higher-priority device may still have to
        give way to several cheaper lower-priority ones if that's what it
        takes to fit.

        Exhaustive over 2^n subsets — n is a handful of managed devices in
        any real installation, so this easily runs every cycle; capped at
        MAX_BATTERY_OPTIMIZATION_DEVICES to guard against a pathologically
        large config, falling back to a simpler independent check (each
        candidate checked alone against the mandatory baseline, no
        cross-candidate trade-offs) above the cap.
        """
        if soc <= min_soc or not candidates:
            return frozenset()

        n = len(candidates)
        if n > MAX_BATTERY_OPTIMIZATION_DEVICES:
            _LOGGER.warning(
                "Battery-optimal set: %d candidate devices exceeds the %d cap, "
                "falling back to a simple independent check for this cycle",
                n, MAX_BATTERY_OPTIMIZATION_DEVICES,
            )
            kept = set()
            for device_id, power, cutoff, _priority in candidates:
                segments = [*mandatory_segments, (power, cutoff)]
                energy = self._project_energy_kwh(
                    segments, now, horizon_end, base_discharge_kw, available_surplus
                )
                if avail_kwh > energy:
                    kept.add(device_id)
            return frozenset(kept)

        def value_of(priority: int) -> int:
            # Exponential, not linear (max_priority_number + 1 - priority):
            # a linear scale lets several lower-priority devices' combined
            # value outweigh one higher-priority device's — e.g. one Prio 1
            # device losing out to four Prio 2-5 devices together, which is
            # the exact opposite of what priority is for. Exponential
            # weighting makes priority *lexicographically* dominant: no
            # combination of every worse-priority device combined can ever
            # outvalue keeping one better-priority device (2^(n-1) always
            # exceeds the sum of every lower power of two), so a device
            # only ever gives way to worse-priority ones once it genuinely
            # doesn't fit on its own — never because enough cheap, low-
            # priority devices happened to add up.
            return 2 ** (max_priority_number - priority)

        best_value = -1
        best_count = -1
        best_subset: tuple[int, ...] = ()
        for mask in range(1 << n):
            indices = [i for i in range(n) if mask & (1 << i)]
            segments = [
                *mandatory_segments,
                *((candidates[i][1], candidates[i][2]) for i in indices),
            ]
            energy_needed = self._project_energy_kwh(
                segments, now, horizon_end, base_discharge_kw, available_surplus
            )
            if energy_needed > avail_kwh:
                continue
            value = sum(value_of(candidates[i][3]) for i in indices)
            count = len(indices)
            # Value first (priority-weighted importance kept on), then
            # count as a tiebreaker (prefer keeping more devices among
            # equally-valuable combinations).
            if value > best_value or (value == best_value and count > best_count):
                best_value = value
                best_count = count
                best_subset = tuple(indices)

        return frozenset(candidates[i][0] for i in best_subset)

    @staticmethod
    def _required_off_cycles(data: CoordinatorData, priority_rank: int = 0) -> int:
        """More battery margin beyond what's needed until solar resumes ->
        wait longer before reacting to a deficit, since it's more likely a
        short-lived spike than a real trend. No margin -> react fast.

        priority_rank (0 = highest priority device) staggers this further:
        each rank below the highest gets STAGGER_CYCLES_PER_PRIORITY_STEP
        fewer cycles, down to OFF_CYCLES_FLOOR. Without this, several
        devices crossing their off-threshold in the same cycle (e.g. solar
        dropping off a cliff at sunset) would all finish their hold at the
        same cycle count and switch off simultaneously instead of shedding
        lowest-priority first.
        """
        margin_h = max(min(data.h_battery, 999.0) - data.effective_h_to_solar, 0.0)
        fraction = min(margin_h / MARGIN_FOR_MAX_PATIENCE_H, 1.0)
        extra = (STABLE_OFF_CYCLES_MAX - STABLE_OFF_CYCLES) * fraction
        base = round(STABLE_OFF_CYCLES + extra)
        staggered = base - priority_rank * STAGGER_CYCLES_PER_PRIORITY_STEP
        return max(staggered, OFF_CYCLES_FLOOR)

    def _get_solar_start(self) -> datetime:
        sun = self.hass.states.get("sun.sun")
        configured_defaults = self._config.get(CONF_SOLAR_OFFSETS, DEFAULT_SOLAR_OFFSETS)
        offsets = self._calibrator.offsets_for(configured_defaults)
        m = dt_util.now().month
        offset_h = offsets[m - 1]
        self._last_offset_h = offset_h

        if sun is None:
            return dt_util.utcnow() + timedelta(hours=12)

        next_rising_raw = sun.attributes.get("next_rising")
        if not next_rising_raw:
            return dt_util.utcnow() + timedelta(hours=12)

        # Accept either a native datetime (how some HA-internal attributes
        # are represented in memory) or an ISO string (how the REST/
        # websocket APIs serialize the same attribute) — see the identical
        # gotcha with schedule.*'s next_event in _effective_cutoff above.
        if isinstance(next_rising_raw, datetime):
            next_rising = next_rising_raw
        elif isinstance(next_rising_raw, str):
            next_rising = dt_util.parse_datetime(next_rising_raw)
        else:
            next_rising = None
        if next_rising is None:
            return dt_util.utcnow() + timedelta(hours=12)

        solar_start_next = next_rising + timedelta(hours=offset_h)
        solar_start_today = solar_start_next - timedelta(hours=24)
        now = dt_util.utcnow()

        # next_rising flips from "today" to "tomorrow" the instant the sun rises.
        # Use today's solar_start if it's still ahead of us, otherwise fall back
        # to the value derived from tomorrow's sunrise.
        return solar_start_today if solar_start_today > now else solar_start_next

    async def _async_update_data(self) -> CoordinatorData:
        # Re-derive the learned solar-start offsets once a day at most — this
        # reads months of statistics and does real computation, far too
        # expensive to repeat every cycle. Independent of the live sensor
        # checks below since it only reads historical statistics.
        if self._calibrator.due_for_recalibration(timedelta(hours=CALIBRATION_INTERVAL_HOURS)):
            await self._calibrator.async_recalibrate()
            diag = self._calibrator.diagnostics
            months = len(diag["kalibrierte_monate"])
            good_days = sum(diag["gute_tage_pro_monat"].values())
            await self._log_system(
                "Solar-Start-Kalibrierung",
                f"{months}/12 Monate kalibriert aus {good_days} guten Tagen",
            )

        # Same idea, much cheaper query: re-derive base_load's floor from
        # the raw house-load sensor's own recent minimum at most once a
        # day — see base_load_floor.py.
        if self._base_load_floor_calibrator.due_for_recalibration(
            timedelta(hours=CALIBRATION_INTERVAL_HOURS)
        ):
            await self._base_load_floor_calibrator.async_recalibrate()

        # Same idea, per wallbox: re-derive its learned max charge rate
        # from its own power sensor's recent history — see
        # wallbox_charge_calibrator.py.
        for wb_calibrator in self._wallbox_charge_calibrators.values():
            if wb_calibrator.due_for_recalibration(timedelta(hours=CALIBRATION_INTERVAL_HOURS)):
                await wb_calibrator.async_recalibrate()

        solar = self._get_core_float(self._config[CONF_SOLAR_SENSOR])
        load = self._get_core_float(self._config[CONF_LOAD_SENSOR])
        soc = self._get_core_float(self._config[CONF_SOC_SENSOR])
        batt = self._get_core_float(self._config[CONF_BATT_SENSOR])
        battery_kwh = self._config.get(CONF_BATTERY_CAPACITY_KWH, 13.8)
        min_soc = self._config.get(CONF_MIN_SOC, 20.0)

        (
            battery_full_missing_kwh,
            battery_full_hours_needed,
            battery_full_hours_until_deadline,
            battery_full_on_track,
        ) = self._battery_full_projection(soc, batt, battery_kwh, dt_util.utcnow())

        discharge = max(-batt, 0.0)
        # h_battery is a division by discharge rate, which would otherwise
        # project a brief spike (e.g. a stove running for 10-15 min) forward
        # as if it continued all night. The median over a 20-*reading*
        # window ignores such a spike almost entirely while still tracking
        # a real, sustained change within roughly half the window's length
        # — genuine readings, not coordinator cycles: only admitted when
        # the raw sensor itself has actually changed (see
        # _last_appended_discharge_kw above), since it only refreshes
        # every ~5 minutes and appending every 60s cycle regardless would
        # let 1-2 real readings dominate a "20-sample" median far more
        # than that name implies.
        if batt != self._last_appended_discharge_kw:
            self._discharge_samples.append(discharge)
            self._last_appended_discharge_kw = batt
        # A single sample right after a composition-change reset (see
        # managed_on_now below) gets no averaging protection at all —
        # bridge to the last value that WAS backed by >=2 independent
        # readings until the fresh deque earns that trust again.
        # Confirmed live: exactly one glitchy reading right after a
        # composition change dragged the overnight discharge projection
        # for ~9 minutes before enough real readings diluted it out.
        if len(self._discharge_samples) >= 2:
            smoothed_discharge = statistics.median(self._discharge_samples)
            self._last_trusted_discharge_kw = smoothed_discharge
        elif self._last_trusted_discharge_kw is not None:
            smoothed_discharge = self._last_trusted_discharge_kw
        else:
            smoothed_discharge = discharge
        avail_kwh = max((soc - min_soc) / 100.0 * battery_kwh, 0.0)
        h_battery = avail_kwh / smoothed_discharge if smoothed_discharge > 0.05 else 999.0

        solar_start = self._get_solar_start()
        now = dt_util.utcnow()
        h_to_solar = max((solar_start - now).total_seconds() / 3600.0, 0.0)

        # See DAYTIME_PROJECTION_HORIZON_H in const.py: h_to_solar points
        # at tomorrow's threshold for the entire rest of today once this
        # morning's has passed, which is a wildly pessimistic horizon for
        # a battery projection while the sun is still actually up.
        sun_state = self.hass.states.get("sun.sun")
        sun_above_horizon = sun_state is not None and sun_state.state == "above_horizon"
        # sun_above_horizon alone is a hard geometric cliff (elevation > 0),
        # not a statement about whether solar is still doing anything —
        # confirmed on a real installation that right at dusk (elevation a
        # fraction of a degree, solar already down to near-zero) it can
        # still read True for several more minutes, during which every
        # device's battery check used the short DAYTIME_PROJECTION_HORIZON_H
        # instead of the real multi-hour overnight one, letting the whole
        # evening's devices pass right up until the exact geometric sunset
        # second — then all switch to the long horizon simultaneously and
        # can discover a real deficit at once. Also requiring solar to
        # still be above SOLAR_START_MIN_KW (the same threshold used to
        # detect the day's solar *start*, applied symmetrically to its
        # end) switches to the honest long horizon a few minutes earlier,
        # as production tapers off, instead of at that one discontinuous
        # instant — smoothing the transition into a gradual, still-
        # sequential shed instead of a last-second cliff. Daytime
        # (sun_above_horizon and meaningful solar) is unaffected.
        effective_h_to_solar = (
            DAYTIME_PROJECTION_HORIZON_H
            if sun_above_horizon and solar >= SOLAR_START_MIN_KW
            else h_to_solar
        )

        batt_ok = h_battery > (effective_h_to_solar + BATT_OK_BUFFER_H) and soc > min_soc

        data = CoordinatorData(
            solar_kw=solar,
            load_kw=load,
            soc=soc,
            batt_kw=batt,
            discharge_kw=discharge,
            smoothed_discharge_kw=smoothed_discharge,
            avail_kwh=avail_kwh,
            h_battery=h_battery,
            h_to_solar=h_to_solar,
            sun_above_horizon=sun_above_horizon,
            effective_h_to_solar=effective_h_to_solar,
            solar_start=solar_start,
            batt_ok=batt_ok,
            min_soc=min_soc,
            calibration=self._calibrator.diagnostics,
            base_load_floor=self._base_load_floor_calibrator.diagnostics,
            wallbox_max_charge={
                dev_id: calibrator.diagnostics
                for dev_id, calibrator in self._wallbox_charge_calibrators.items()
            },
            active_solar_offset_h=self._last_offset_h,
            next_cycle_at=dt_util.utcnow() + timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            battery_full_missing_kwh=battery_full_missing_kwh,
            battery_full_hours_needed=battery_full_hours_needed,
            battery_full_hours_until_deadline=battery_full_hours_until_deadline,
            battery_full_on_track=battery_full_on_track,
        )

        await self._evaluate_devices(data)
        return data

    async def _evaluate_devices(self, data: CoordinatorData) -> None:
        """Cascade surplus across devices in priority order.

        The wallbox is excluded from switching (it's controlled by its own PV
        logic already) but its measured power is subtracted from the load so
        it doesn't count as "unavoidable base load" for our devices.
        """
        all_devices = self.devices
        devices_by_id = {d["_id"]: d for d in all_devices}
        wallbox_devices = [d for d in all_devices if d.get(CONF_DEVICE_IS_WALLBOX, False)]
        candidate_devices = [d for d in all_devices if not d.get(CONF_DEVICE_IS_WALLBOX, False)]

        wallbox_power_kw = sum(
            self._get_power_kw(wb.get(CONF_DEVICE_POWER_SENSOR)) for wb in wallbox_devices
        )

        # Figure out which candidate devices are currently on, and how much
        # power they're drawing right now, so we can subtract that from the
        # house load and recover the "base load" our devices don't control.
        # A device is either switch-controlled or climate-controlled (e.g. a
        # pool heat pump with only a thermostat mode, no on/off switch) —
        # device_control handles both uniformly.
        device_is_on: dict[str, bool] = {}
        managed_power_kw = 0.0
        for dev in candidate_devices:
            device_id = dev["_id"]
            is_on = is_device_on(self.hass, dev)
            device_is_on[device_id] = is_on
            if is_on:
                sensor_id = dev.get(CONF_DEVICE_POWER_SENSOR)
                managed_power_kw += (
                    self._get_power_kw(sensor_id) if sensor_id else dev.get(CONF_DEVICE_POWER_KW, 0.15)
                )

        # A managed device turning on/off changes the discharge rate
        # immediately and predictably — it's not the kind of noise the
        # smoothing median is meant to filter (that's for *external*
        # spikes, like a kettle). Without this reset, base_discharge_kw
        # below would keep mostly reflecting the pre-change composition
        # for up to ~20 minutes (the full smoothing window), e.g. still
        # looking like the battery is draining fast right after a
        # windowed device's cutoff actually frees up that margin.
        managed_on_now = frozenset(dev_id for dev_id, on in device_is_on.items() if on)
        if managed_on_now != self._last_managed_on:
            self._discharge_samples.clear()
            self._base_load_samples.clear()
            self._last_appended_load_kw = None
            self._last_appended_discharge_kw = None
        self._last_managed_on = managed_on_now

        # Our own switch/climate states react within seconds of a
        # transition (window/schedule cutoff, dependency, surplus
        # decision), but the house-load and battery-discharge sensors are
        # a cloud-polled integration that only refreshes every few minutes
        # (observed: ~5 min lag on FusionSolarPlus, confirmed on both). A
        # fresh last_changed/value doesn't by itself prove the reading has
        # settled into the post-transition reality either — this system
        # has been observed producing a low-looking-but-still-transitional
        # value right as it mid-refreshes. Right after a managed device
        # turns off, subtracting the fresh (lower) managed_power_kw from a
        # load/discharge reading that still reflects the pre-transition
        # situation misattributes the device's own lingering draw to
        # "base load"/"unavoidable discharge", spiking both and tanking
        # available_surplus until the sensors genuinely catch up. This
        # happens every evening a windowed device (e.g. the pool pump)
        # hits its cutoff, not just occasionally.
        #
        # Keep using the managed-power figure from just before a
        # composition change until each consumer's own sensor has
        # produced at least STALENESS_MIN_REFRESHES genuinely new
        # readings since — real evidence it's cycled past the transition,
        # rather than guessing a fixed delay — capped by
        # LOAD_SENSOR_STALENESS_GRACE in case a sensor stalls and never
        # reaches that count. Two independent freezes (see __init__).
        now = dt_util.utcnow()

        # self._last_managed_power_kw starts at 0.0 (see __init__) purely as
        # a Python default, not because devices were actually off — but the
        # check below can't tell the difference. Without _managed_power_kw_seen,
        # the very first cycle after every integration (re)start/reload (e.g.
        # right after installing an update) sees "0.0 -> real managed power"
        # and mistakes that for a genuine composition change, freezing
        # effective_managed_power_kw at the fictitious 0.0 baseline for up to
        # LOAD_SENSOR_STALENESS_GRACE — during which base_load/base_discharge_kw
        # wrongly include the full house load, as if no managed device were
        # subtracted at all, making the battery projection needlessly
        # pessimistic (and devices needlessly quick to shed) right after
        # every restart. The first real reading is always trustworthy on its
        # own — there's no "before" to protect against yet — so it's taken
        # as-is instead of being run through the freeze logic.
        if not self._managed_power_kw_seen:
            self._managed_power_kw_seen = True
            self._last_managed_power_kw = managed_power_kw

        composition_changed = managed_power_kw != self._last_managed_power_kw
        if composition_changed and self._stale_managed_power_kw_load is None:
            # Only capture a fresh freeze point if we're not already mid
            # grace-period — a second device changing before load_kw has
            # caught up with the first (e.g. the pool pump and its
            # dependent heat pump both hitting their cutoff within the
            # same minute) must not overwrite the original pre-cluster
            # value with an intermediate one load_kw never actually
            # reflected either.
            self._stale_managed_power_kw_load = self._last_managed_power_kw
            self._stale_since_load = now
            self._last_seen_load_kw = data.load_kw
            self._load_refresh_count = 0
        if composition_changed and self._stale_managed_power_kw_discharge is None:
            self._stale_managed_power_kw_discharge = self._last_managed_power_kw
            self._stale_since_discharge = now
            self._last_seen_discharge_kw = data.discharge_kw
            self._discharge_refresh_count = 0

        effective_managed_power_kw = managed_power_kw
        if self._stale_managed_power_kw_load is not None:
            if data.load_kw != self._last_seen_load_kw:
                self._load_refresh_count += 1
                self._last_seen_load_kw = data.load_kw
            caught_up = self._load_refresh_count >= STALENESS_MIN_REFRESHES
            timed_out = now - self._stale_since_load >= LOAD_SENSOR_STALENESS_GRACE
            if caught_up or timed_out:
                self._stale_managed_power_kw_load = None
                self._stale_since_load = None
            else:
                effective_managed_power_kw = self._stale_managed_power_kw_load

        effective_managed_power_kw_discharge = managed_power_kw
        if self._stale_managed_power_kw_discharge is not None:
            if data.discharge_kw != self._last_seen_discharge_kw:
                self._discharge_refresh_count += 1
                self._last_seen_discharge_kw = data.discharge_kw
            caught_up = self._discharge_refresh_count >= STALENESS_MIN_REFRESHES
            timed_out = now - self._stale_since_discharge >= LOAD_SENSOR_STALENESS_GRACE
            if caught_up or timed_out:
                self._stale_managed_power_kw_discharge = None
                self._stale_since_discharge = None
            else:
                effective_managed_power_kw_discharge = self._stale_managed_power_kw_discharge

        self._last_managed_power_kw = managed_power_kw

        # Floored at the house's own recent minimum draw, not a hard 0.0 —
        # a household never genuinely idles at 0 kW (fridge, standby,
        # networking gear), and several managed devices without a real
        # power sensor fall back to a static config *estimate* that can
        # briefly overshoot their real draw, which would otherwise send
        # this negative and floor it at a physically implausible 0 —
        # making base_discharge_kw below look more favorable than reality
        # for that cycle. See base_load_floor.py.
        base_load = max(
            data.load_kw - wallbox_power_kw - effective_managed_power_kw,
            self._base_load_floor_calibrator.floor_kw,
        )
        available_surplus = data.solar_kw - base_load

        data.base_load_kw = base_load
        # The true, physical surplus — captured before any wallbox
        # reservation narrows what the device cascade gets to see below,
        # so the diagnostic sensor always reflects reality, not an
        # artificially-shrunk figure. Raw base_load is exactly right
        # here — this is the device cascade's own moment-to-moment
        # surplus check, which should react immediately to a cloud
        # clearing, not lag behind a rolling median.
        data.surplus_kw = available_surplus

        # smoothed_base_load: same reasoning as smoothed_discharge_kw
        # above, but NOT the same "once per 60s cycle" admission —
        # data.load_kw itself is a cloud-polled reading that only
        # actually changes every ~5 minutes (confirmed directly against
        # real data: 20:40:00, 20:41:27, 20:45:30, 20:50:20, 20:55:22 —
        # roughly 4-5.5 min apart), so appending unconditionally every
        # cycle would fill "20 samples" with only ~4 genuinely distinct
        # readings, each duplicated 4-5x — letting 1-2 real bad readings
        # dominate a median that looks far more robust than it actually
        # is. Only admitting a sample when the raw reading itself has
        # genuinely changed makes the window mean what its name says
        # (DISCHARGE_SMOOTHING_SAMPLES genuinely independent
        # measurements, spanning however long that takes in wall-clock
        # time) regardless of the coordinator's own polling cadence or
        # any drift in how often the cloud source actually refreshes.
        if data.load_kw != self._last_appended_load_kw:
            self._base_load_samples.append(base_load)
            self._last_appended_load_kw = data.load_kw
        # Same composition-change bridge as smoothed_discharge above —
        # the first sample into a freshly-reset deque is otherwise fully
        # exposed, right when it's most likely to be transiently wrong.
        if len(self._base_load_samples) >= 2:
            smoothed_base_load = statistics.median(self._base_load_samples)
            self._last_trusted_base_load = smoothed_base_load
        elif self._last_trusted_base_load is not None:
            smoothed_base_load = self._last_trusted_base_load
        else:
            smoothed_base_load = base_load

        # Narrows what the cascade below actually competes over — each
        # wallbox's own reservation is already capped at the surplus
        # that exists, so this can't push available_surplus any lower
        # than "nothing left for anyone", never negative beyond that.
        #
        # Fed smoothed_available_surplus, not the raw available_surplus
        # above — the reservation is a *rate* meant to hold for hours
        # until the target is reached, the same kind of multi-hour
        # figure as the overnight battery projection below, not an
        # instant on/off decision. Feeding it raw surplus meant a cloud
        # passing over for two minutes — or the wallbox's own draw
        # beating on a laggy ~5-min-cadence house-load reading, same
        # staleness smoothed_base_load exists to filter — directly
        # became a jump in the reserved kW shown on the dashboard, even
        # though what actually changed (the car's still-missing kWh, the
        # hours left to get there) barely moved at all. Confirmed live:
        # reserved kW swinging between ~2 and ~10 within a single hour
        # while the underlying deficit was constant.
        smoothed_available_surplus = data.solar_kw - smoothed_base_load
        wallbox_reserved_kw = sum(
            self._wallbox_reserved_kw(wb, now, smoothed_available_surplus)
            for wb in wallbox_devices
            if wb.get(CONF_WALLBOX_CAPACITY_ENTITY)
        )
        # True only when the reservation consumed essentially the whole
        # surplus that existed before it — capped by *availability*, not
        # by the wallbox's own max-charge-rate or a small fractional
        # need. Only in this state has every watt genuinely been claimed:
        # a device granted "on" below purely because the battery could
        # afford it would draw power the inverter's own surplus-based
        # wallbox charging would otherwise route to the car — confirmed
        # live, the car's charge rate visibly rises the moment SLS's
        # managed devices go off. "The battery can afford it" and "the
        # wallbox doesn't need it more" are different questions; this
        # flag answers the second one. See its use against
        # battery_eligible_ids below.
        wallbox_starved = (
            wallbox_reserved_kw > 0.0
            and wallbox_reserved_kw >= smoothed_available_surplus - 0.05
        )
        # Debounced: takes effect on the very first starved cycle (never
        # delay protecting the wallbox), but only releases once
        # wallbox_starved has read False for WALLBOX_RELIEF_CYCLES in a
        # row. Without this, a single clear cycle on a partly-cloudy day
        # would flip battery_eligible_ids back open, which resets every
        # device's own off_counter (see the tracker below — it zeroes on
        # any should_on cycle) right as it was accumulating toward
        # actually switching off. A device could then never accumulate
        # enough *consecutive* should_off cycles to switch, even though
        # the wallbox was starved the clear majority of the time.
        if wallbox_starved:
            self._wallbox_relief_counter = 0
        else:
            self._wallbox_relief_counter = min(
                self._wallbox_relief_counter + 1, WALLBOX_RELIEF_CYCLES
            )
        wallbox_starved_effective = (
            wallbox_starved or self._wallbox_relief_counter < WALLBOX_RELIEF_CYCLES
        )
        available_surplus -= wallbox_reserved_kw
        data.wallbox_reserved_kw = wallbox_reserved_kw

        # Diagnostic only for now — see load_profile.py. Sampled here
        # (not gated on day/night or anything else) so every cycle
        # contributes, same as base_load's own display value.
        hausmodus_entity = self._config.get(CONF_HAUSMODUS_ENTITY)
        hausmodus_state = self.hass.states.get(hausmodus_entity) if hausmodus_entity else None
        hausmodus = (
            hausmodus_state.state
            if hausmodus_state is not None and hausmodus_state.state not in ("unavailable", "unknown")
            else None
        )
        self._load_profile_learner.record(base_load, hausmodus)
        data.load_profile = self._load_profile_learner.diagnostics

        # Computed once per cycle, not per dependent device — several
        # devices could depend on the same wallbox, and calling
        # _wallbox_satisfied once per dependent would advance its idle
        # hold-time counter faster than once per cycle.
        wallbox_satisfied = {
            wb["_id"]: self._wallbox_satisfied(wb) for wb in wallbox_devices
        }

        # Battery discharge currently attributable to managed devices already
        # running is whatever part of their draw a *positive* surplus doesn't
        # cover — a negative surplus (base load alone exceeding solar) isn't
        # their doing and must not be clamped away here, or a negative
        # available_surplus with zero managed devices running would wrongly
        # attribute the base-load deficit to "managed devices". Subtracting
        # their real contribution from the measured discharge leaves the
        # "unavoidable" base discharge — what the battery would still be
        # losing even with every managed device off. This is the foundation
        # for a per-device, forward-looking battery projection below.
        #
        # The 20-minute median smoothing exists to tell a brief external
        # spike (kettle, oven) apart from a real sustained change — but a
        # managed device turning on/off *is* a real, immediate, known
        # composition change, not noise to smooth over. If we didn't reset
        # here, the median would keep mostly reflecting the discharge rate
        # from before the change for up to ~20 minutes after e.g. a
        # windowed device's cutoff, understating how much margin just
        # opened up (see the "which_on changed" reset below).
        #
        # This must use effective_managed_power_kw_discharge — its own,
        # independently-gated staleness-corrected figure (see __init__ and
        # the freeze logic above), not the fresh managed_power_kw —
        # confirmed directly against real data that the battery charge/
        # discharge sensor lags on the same ~5-minute cloud polling
        # cadence as the load sensor (both come from the same
        # FusionSolarPlus source). Right after a composition change, the
        # deque above was just cleared and gets refilled starting from
        # data.discharge_kw, itself a still-stale (pre-transition) reading
        # for the same several minutes. Subtracting the fresh (post-
        # transition, lower) managed_power_kw from that stale reading
        # would then attribute most of it to "unavoidable" base discharge
        # instead of to the devices that, as far as this still-lagging
        # sensor is concerned, are still running — inflating
        # base_discharge_kw and making the battery projection needlessly
        # pessimistic for every device right when a windowed device's
        # cutoff should be making things easier, not harder.
        managed_discharge_kw = max(
            effective_managed_power_kw_discharge - max(available_surplus, 0.0), 0.0
        )
        if data.sun_above_horizon:
            # Floored at the same learned floor as base_load, not a hard
            # 0.0 — a household never genuinely idles at 0 kW, and this
            # same live-attribution formula can read as "fully covered"
            # whenever a managed device's config *estimate* (no real power
            # sensor) briefly overshoots its real draw, for the identical
            # reason base_load's own floor exists. See base_load_floor.py.
            base_discharge_kw = max(
                data.smoothed_discharge_kw - managed_discharge_kw,
                self._base_load_floor_calibrator.floor_kw,
            )
        else:
            # At dusk, solar can sit briefly right around base_load_kw
            # (available_surplus ≈ 0) purely by coincidence of a rapidly
            # *declining* reading passing through that value on its way
            # to zero — the live-attribution formula above reads that
            # instant as "base load fully covered, nothing unavoidable
            # right now", which is technically true for that one moment
            # but then gets extrapolated as a flat rate across the
            # entire multi-hour overnight projection in
            # _hours_until_depleted below. Once the sun's below the
            # horizon, solar isn't coming back until tomorrow regardless
            # of what it still reads this instant, so the durable
            # overnight "unavoidable" rate is simply the household's own
            # base load, undiminished by a solar contribution that's
            # already on its way out. Smoothed (see smoothed_base_load
            # above), not the raw instantaneous reading — this rate gets
            # multiplied by every remaining hour until solar start, so
            # unsmoothed noise here is what was flipping borderline
            # devices' feasibility verdict every cycle all night.
            base_discharge_kw = smoothed_base_load

        device_states: dict[str, bool] = {}
        device_diagnostics: dict[str, DeviceDiagnostics] = {}
        cumulative_committed = 0.0
        committed_segments: list[tuple[float, datetime | None]] = []
        now_dt = dt_util.utcnow()
        horizon_end = now_dt + timedelta(hours=data.effective_h_to_solar + BATT_OK_BUFFER_H)

        # Read-only pre-pass mirroring the main loop's hard-boundary checks
        # below (legacy off-only / window-far-closed / blocked),
        # just to sort every device into exactly one of three buckets before
        # any decision is made, for _select_battery_optimal_set:
        #   - mandatory_segments: always-on regardless of the battery check
        #     (disabled-but-physically-running, or minimum-runtime-forced)
        #   - optional_candidates: genuinely competing for battery budget
        #     this cycle — eligible to actually turn on/stay on
        #   - (implicitly excluded): hard-blocked or blocked-but-off
        #     devices, which can't commit real budget either way and are
        #     evaluated the old, simpler sequential way further down, only
        #     for their own pre-charge countdown.
        # See _select_battery_optimal_set's docstring for why this matters.
        mandatory_segments: list[tuple[float, datetime | None]] = []
        optional_candidates: list[tuple[str, float, datetime | None, int]] = []
        max_priority_number = 1
        for _dev in candidate_devices:
            _device_id = _dev["_id"]
            if not control_entity_id(_dev):
                continue
            _is_on = device_is_on[_device_id]
            _predicted_power, _ = self._predicted_power_kw(_dev)
            _own_cutoff = self._effective_cutoff(_dev, now_dt, devices_by_id)
            _priority = _dev.get(CONF_DEVICE_PRIORITY, 99)
            max_priority_number = max(max_priority_number, _priority)

            if not _dev.get(CONF_DEVICE_ENABLED, True):
                if _is_on:
                    mandatory_segments.append((_predicted_power, _own_cutoff))
                continue

            _in_window = self._in_window(_dev)
            if _in_window is None and _dev.get(CONF_DEVICE_OFF_ONLY, False):
                continue

            _window_closed = _in_window is False
            _precharge_horizon = timedelta(seconds=STABLE_ON_CYCLES * UPDATE_INTERVAL_SECONDS)
            if _window_closed and not _is_on and not self._window_reopens_within(
                _dev, now_dt, _precharge_horizon
            ):
                continue

            _depends_on_id = _dev.get(CONF_DEVICE_DEPENDS_ON)
            _depends_on_dev = devices_by_id.get(_depends_on_id) if _depends_on_id else None
            if _depends_on_dev is not None and _depends_on_dev.get(CONF_DEVICE_IS_WALLBOX, False):
                _dependency_met = wallbox_satisfied.get(_depends_on_id, True)
            else:
                _dependency_met = _depends_on_id is None or device_is_on.get(_depends_on_id, False)

            # _force_runtime has to be known before _soc_too_low below,
            # since it suspends the SOC floor — computed here (earlier
            # than the main loop computes its own copy) purely for that
            # ordering reason.
            _runtime_tracker = self._runtime_trackers.get(_device_id)
            _runtime_hours_today = _runtime_tracker.hours_today if _runtime_tracker is not None else 0.0
            _force_runtime = self._force_runtime_active(
                _dev, now_dt, _runtime_hours_today, _predicted_power
            )

            _blocked = _window_closed or not _dependency_met
            if _blocked:
                # Either forced off this cycle (blocked and on) or just
                # pre-charging (blocked and off) — neither commits real
                # budget, handled the old sequential way further down.
                continue
            if _force_runtime:
                mandatory_segments.append((_predicted_power, _own_cutoff))
                continue

            _device_min_soc = _dev.get(CONF_DEVICE_MIN_SOC_PERCENT)
            _soc_too_low = (
                _device_min_soc is not None
                and data.soc < _device_min_soc
                and not _force_runtime
            )
            if _soc_too_low:
                # Below its own reserve floor: excluded from the battery-
                # budget competition entirely, so it can never win
                # "battery_would_last" while under the floor — but unlike
                # window/dependency this never blocks it from turning on
                # via direct PV surplus in the main loop below, which
                # doesn't consult battery_eligible_ids at all. The floor
                # is purely a "don't let the battery cover this device"
                # guarantee, not an on/off gate of its own.
                continue

            optional_candidates.append((_device_id, _predicted_power, _own_cutoff, _priority))

        battery_eligible_ids = self._select_battery_optimal_set(
            optional_candidates, mandatory_segments, now_dt, horizon_end,
            base_discharge_kw, available_surplus, data.avail_kwh, data.soc, data.min_soc,
            max_priority_number,
        )

        if wallbox_starved_effective:
            # Every device below would run on battery, not on live
            # surplus, while the wallbox is still short of what it needs
            # right now (or was, within the last WALLBOX_RELIEF_CYCLES —
            # see wallbox_starved_effective above). force_runtime
            # devices are unaffected: they never go through
            # battery_eligible_ids, they're already in mandatory_segments
            # and win via their own should_on branch further down.
            battery_eligible_ids = frozenset()

        # A device *re-joining* the set (wasn't eligible last cycle) must
        # also clear a more comfortable margin than the one that excluded
        # it — otherwise, right at the edge, the horizon shrinking by a
        # minute every cycle (solar start getting a minute closer) is
        # enough on its own to flip a razor-thin verdict back and forth,
        # with zero change in any real sensor reading. Confirmed on a real
        # installation: a device shed with a 0.01h margin was granted
        # eligibility again 2 cycles later purely from time passing.
        # Shedding itself is unaffected — this only ever pulls devices OUT
        # of what the normal (stricter) horizon already granted, never
        # adds anything beyond it, so it can't make the projection less
        # safe.
        candidate_ids = {c[0] for c in optional_candidates}
        previously_eligible = self._last_battery_eligible_ids & candidate_ids
        newly_added = battery_eligible_ids - previously_eligible
        if newly_added:
            winning_segments = [
                *mandatory_segments,
                *(
                    (c[1], c[2]) for c in optional_candidates
                    if c[0] in battery_eligible_ids
                ),
            ]
            comfortable_horizon_end = horizon_end + timedelta(hours=RE_INCLUSION_COMFORT_BUFFER_H)
            energy_with_buffer = self._project_energy_kwh(
                winning_segments, now_dt, comfortable_horizon_end,
                base_discharge_kw, available_surplus,
            )
            if energy_with_buffer > data.avail_kwh:
                # Not comfortable enough yet — hold the newly-joining
                # devices back this cycle, keep only what was already
                # proven eligible (a subset of a feasible set is always
                # itself feasible, so this stays safe under the normal
                # horizon too).
                battery_eligible_ids = battery_eligible_ids & previously_eligible

        self._last_battery_eligible_ids = battery_eligible_ids

        for priority_rank, dev in enumerate(candidate_devices):
            device_id = dev["_id"]
            control_id = control_entity_id(dev)
            is_on = device_is_on[device_id]
            device_states[device_id] = is_on

            # Feed the rolling average while the device is actually drawing power
            sensor_id = dev.get(CONF_DEVICE_POWER_SENSOR)
            if is_on and sensor_id:
                tracker = self._power_trackers.get(device_id)
                if tracker is not None:
                    tracker.add_sample(self._get_power_kw(sensor_id))

            # Feed today's accumulated runtime regardless of whether a
            # minimum is configured, so history already exists if one is
            # added later.
            runtime_tracker = self._runtime_trackers.get(device_id)
            if runtime_tracker is not None:
                runtime_tracker.add_cycle(is_on, UPDATE_INTERVAL_SECONDS)
            runtime_hours_today = runtime_tracker.hours_today if runtime_tracker is not None else 0.0

            predicted_power, diag = self._predicted_power_kw(dev)
            diag.is_on = is_on
            diag.priority = dev.get(CONF_DEVICE_PRIORITY, 99)
            diag.runtime_hours_today = runtime_hours_today
            in_window = self._in_window(dev)
            legacy_off_only = dev.get(CONF_DEVICE_OFF_ONLY, False)
            diag.off_only = legacy_off_only or in_window is not None

            # A minimum daily runtime is only ever *forced* (i.e. may draw
            # grid power) from the afternoon onward, once it's clear a good
            # surplus morning alone won't reach the target — a device is
            # never denied its normal surplus/battery-driven chance to reach
            # the target for free earlier in the day.
            min_daily_runtime_h = dev.get(CONF_DEVICE_MIN_DAILY_RUNTIME_H)
            force_runtime = self._force_runtime_active(
                dev, now_dt, runtime_hours_today, predicted_power
            )
            diag.force_runtime = force_runtime

            # Some devices physically can't do anything unless another
            # device is already running — e.g. a heat pump with a flow
            # switch that only lets the compressor start while its
            # circulation pump has water moving. Without this, we could
            # command such a device "on" for nothing (wasting its reserved
            # cascade budget) and its power samples would be diluted by long
            # idle-but-"on" stretches, dragging down the measured average.
            #
            # A wallbox is a special case: it's never itself switched by
            # the cascade (see candidate_devices above), so "is it on"
            # means nothing for it — depending on one instead means "is the
            # car satisfied" (see _wallbox_satisfied). This is what lets a
            # lower-priority device like a pool heat pump hold back while a
            # car still badly needs the surplus, without giving the wallbox
            # its own cascade priority or switching it.
            depends_on_id = dev.get(CONF_DEVICE_DEPENDS_ON)
            depends_on_dev = devices_by_id.get(depends_on_id) if depends_on_id else None
            if depends_on_dev is not None and depends_on_dev.get(CONF_DEVICE_IS_WALLBOX, False):
                dependency_met = wallbox_satisfied.get(depends_on_id, True)
            else:
                dependency_met = depends_on_id is None or device_is_on.get(depends_on_id, False)
            diag.dependency_met = dependency_met

            # A device can be disabled entirely via its "— Aktiviert" switch
            # — hands-off: the cascade never reserves budget for it and
            # never actuates it either way, leaving it exactly as it is for
            # manual or other-automation control. Config and historical
            # power/runtime data are untouched, so the device picks up
            # right where it left off once re-enabled.
            device_enabled = dev.get(CONF_DEVICE_ENABLED, True)
            diag.enabled = device_enabled

            device_diagnostics[device_id] = diag

            if not control_id:
                # No switch or climate entity configured (shouldn't happen
                # for non-wallbox devices — validated at config time),
                # nothing to actuate.
                continue

            tracker = self._device_trackers.setdefault(
                device_id, DeviceState(device_id=device_id)
            )

            if not device_enabled:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                if is_on:
                    # Hands-off, but still actually running right now
                    # (manual or another automation) — its current draw is
                    # already excluded from base_discharge_kw like any
                    # other running device (see managed_power_kw above),
                    # but the forward-looking battery projection has no
                    # way to know it'll stop unless we tell it: without
                    # this, a device left running past its usual schedule
                    # window reads as a permanent addition to the
                    # unavoidable load for the rest of the night instead
                    # of dropping off at its own configured cutoff, same
                    # as it would if the cascade still controlled it. This
                    # only feeds the projection — the device itself is
                    # still never touched here.
                    own_cutoff = self._effective_cutoff(dev, now_dt, devices_by_id)
                    committed_segments.append((predicted_power, own_cutoff))
                await self._log_decision(
                    dev, False, "Deaktiviert",
                    "deaktiviert — wird nicht angesteuert (manuelle/andere Steuerung)",
                )
                continue

            # A device configured with the legacy off_only flag and no
            # window behaves like a window that's always closed.
            if in_window is None and legacy_off_only:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                await self._log_decision(
                    dev, False, "Kein Zeitfenster",
                    "kein Zeitfenster konfiguriert und off_only gesetzt",
                )
                if is_on:
                    await async_turn_off(self.hass, dev)
                continue

            # A closed window or an unmet prerequisite dependency is still
            # a hard boundary for actually *running* the device — while
            # it's on, either one is enforced immediately, no hysteresis,
            # same as before.
            window_closed = in_window is False
            precharge_horizon = timedelta(seconds=STABLE_ON_CYCLES * UPDATE_INTERVAL_SECONDS)
            window_far_closed = window_closed and not self._window_reopens_within(
                dev, now_dt, precharge_horizon
            )

            # A window that just closed for the day (next open likely
            # tomorrow, well beyond the pre-charge horizon) is a hard
            # boundary — no point pre-charging on_counter for a reopening
            # hours away. Without this, a device sits primed (and shows a
            # misleading "wartet noch X min bis einschalten" countdown)
            # for the rest of the evening right after every window close,
            # even though it can't actually turn on until blocked clears
            # regardless.
            #
            # An unmet dependency deliberately does NOT get this treatment
            # — pre-charging on_counter while waiting on a dependency is
            # kept on purpose (the user's explicit choice: a device like
            # Pool-WP should be ready to join *instantly* the moment its
            # dependency clears, not wait out a fresh ~10-cycle hold
            # afterward, even though that means it can sit "primed" for a
            # while with nothing visibly changing). See diag.dependency_met
            # / _switch_countdown in sensor.py for how the *display* still
            # tells this apart from a genuine imminent switch-on.
            if window_far_closed and not is_on:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                await self._log_decision(
                    dev, False, "Außerhalb Zeitfenster",
                    "außerhalb des Zeitfensters — öffnet erst wieder später, kein Vorladen",
                )
                continue

            remaining_surplus = available_surplus - cumulative_committed

            # An optional per-device SOC floor. Deliberately has NO
            # instant/hard-cutoff path of its own (unlike window_closed
            # or unmet dependency below) — its only effect is upstream,
            # in the pre-pass above, which excludes a device under its
            # floor from battery_eligible_ids so it can never win
            # "battery_would_last" down there. That alone is enough to
            # make the normal should_off further below fire once surplus
            # genuinely can't cover it, going through the SAME multi-
            # cycle off_counter debounce as every other shutdown reason —
            # no separate flapping-prone hard-off needed. An earlier
            # version DID hard-cut a soc-too-low device the instant it
            # turned on, which produced confirmed live on/off cycling
            # every ~10 minutes on an otherwise stable, cloudless morning
            # (see project memory): floors set high enough to be "always
            # below" for most of the morning turned every one-cycle
            # surplus blip into an instant, undebounced cutoff, wiping
            # out the normal hysteresis this codebase otherwise relies on
            # everywhere else. Suspended while force_runtime is active
            # (the user's explicit choice: a minimum daily runtime target
            # may dip into this reserve rather than never being reached
            # on a low-SOC day) — handled entirely by the pre-pass, which
            # already knows force_runtime by the time it excludes/admits
            # a device.
            device_min_soc = dev.get(CONF_DEVICE_MIN_SOC_PERCENT)
            soc_too_low = (
                device_min_soc is not None
                and data.soc < device_min_soc
                and not force_runtime
            )
            diag.device_min_soc = device_min_soc

            blocked = window_closed or not dependency_met
            if blocked and is_on:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                titel = "Außerhalb Zeitfenster" if window_closed else "Abhängigkeit nicht erfüllt"
                reason = (
                    "außerhalb des Zeitfensters" if window_closed
                    else "Abhängigkeit nicht erfüllt (Voraussetzung läuft nicht)"
                )
                await self._log_decision(dev, False, titel, reason)
                _LOGGER.info("PV Surplus: turning OFF %s (%s)", dev.get(CONF_DEVICE_NAME), reason)
                await async_turn_off(self.hass, dev)
                continue

            # Blocked (by window or dependency) but the device is already
            # off is *not* a hard exit anymore: the surplus/battery
            # judgement below still runs and on_counter still charges
            # normally, so a device that's been qualifying the whole time
            # switches on the instant the window opens or the dependency
            # clears, instead of waiting out a fresh multi-minute hold
            # from zero afterward. Only the actual switch-on call and the
            # cascade-budget reservation are suppressed while blocked —
            # see the two `not blocked` guards below.

            # Forward-looking battery check: would the battery still last
            # until solar start if THIS device — on top of every
            # higher-priority device already committed above — draws its
            # predicted power, with whatever isn't covered by surplus
            # coming from the battery? This replaces a single global
            # "is the battery currently discharging" flag, which caused two
            # problems: (1) a device could turn ON because the battery
            # *happened* not to be discharging yet, then immediately start
            # draining it once the load actually kicked in, flipping the
            # decision back and forth every few minutes; (2) every device
            # shared the same flag, so when it flipped, all of them turned
            # off together instead of shedding lowest-priority load first.
            #
            # The projection is also time-window-aware: a committed device
            # with a known cutoff (its own schedule/window, or inherited
            # from a prerequisite it depends on) drops out of the load at
            # that point instead of being assumed to draw power all the way
            # to solar start — otherwise a lower-priority device's
            # projection stays needlessly pessimistic after a
            # higher-priority windowed device is due to stop anyway.
            own_cutoff = self._effective_cutoff(dev, now_dt, devices_by_id)
            # Only shown when it actually falls inside the current
            # projection horizon (same strict "<" _project_energy_kwh uses
            # to decide whether a cutoff creates a real segment boundary)
            # — a cutoff beyond the horizon (e.g. a "stops overnight"
            # device's rolling +2h during a sunny midday, when the whole
            # horizon is only ~1.5h) doesn't affect anything this cycle,
            # and showing it anyway reads as "this device stops soon" when
            # in reality it's just an unused future value. Confirmed
            # matching a real installation's confusion: a device's cutoff
            # displaying every morning even though it only ever binds
            # once the evening's long horizon kicks in.
            diag.effective_cutoff = (
                own_cutoff.isoformat() if own_cutoff and own_cutoff < horizon_end else None
            )
            if blocked:
                # Blocked-but-off (window not open yet / dependency unmet):
                # can't actually commit real budget this cycle regardless
                # of the answer, so it wasn't part of the battery-optimal
                # set search above — checked the old, simpler sequential
                # way instead, purely so its own pre-charge countdown has
                # something to go on.
                projected_segments = [*committed_segments, (predicted_power, own_cutoff)]
                energy_needed_kwh = self._project_energy_kwh(
                    projected_segments, now_dt, horizon_end, base_discharge_kw, available_surplus
                )
                battery_would_last = data.avail_kwh > energy_needed_kwh and data.soc > data.min_soc
            else:
                # Genuinely competing for tonight's battery budget — use
                # the priority-optimal combination picked above instead of
                # a purely sequential per-device check, so a higher-
                # priority device with no cutoff (assumed to draw power
                # all the way to solar start) doesn't lose out to a lower-
                # priority device that merely happens to have a bounded
                # schedule, when shedding the lower-priority one wouldn't
                # even have helped. See _select_battery_optimal_set.
                battery_would_last = device_id in battery_eligible_ids
            required_off_cycles = self._required_off_cycles(data, priority_rank)
            diag.required_off_cycles = required_off_cycles
            diag.required_on_cycles = STABLE_ON_CYCLES

            should_on = (
                force_runtime
                or (remaining_surplus > predicted_power + SURPLUS_ON_THRESHOLD)
                or battery_would_last
            )
            should_off = (
                not force_runtime
                and (remaining_surplus < predicted_power + SURPLUS_OFF_THRESHOLD)
                and not battery_would_last
            )
            # In the small hysteresis dead zone between the on/off
            # thresholds, neither condition holds — the target is simply
            # "stay as you are", not a deviation either way. Forced to
            # False while blocked (window closed or dependency unmet)
            # regardless of what the surplus/battery judgement says — the
            # device genuinely isn't allowed to run yet, pre-charging
            # on_counter below is just getting it ready for the moment
            # it is.
            diag.should_be_on = (should_on if (should_on or should_off) else is_on) and not blocked

            if should_on and blocked:
                wartet_auf = "den Start des Zeitfensters" if window_closed else "die Abhängigkeit"
                decision_reason = (
                    f"Überschuss/Akku würden bereits ausreichen ("
                    f"{remaining_surplus:.2f} kW verfügbar, {predicted_power:.2f} kW "
                    f"benötigt) — bereit, wartet noch auf {wartet_auf}"
                )
                titel = (
                    "Vorbereitet — wartet auf Zeitfenster" if window_closed
                    else "Bereit — wartet auf Abhängigkeit"
                )
                await self._log_decision(dev, False, titel, decision_reason)
            elif should_on:
                if force_runtime:
                    decision_titel = "Einschalten — Mindest-Laufzeit"
                    decision_reason = (
                        f"Mindest-Laufzeit erzwungen ({runtime_hours_today:.1f}h/"
                        f"{min_daily_runtime_h:.1f}h heute erreicht)"
                    )
                elif battery_would_last:
                    decision_titel = "Einschalten — Akku reicht"
                    decision_reason = (
                        f"Akku würde bis Solar-Start reichen (auch mit "
                        f"{predicted_power:.2f} kW zusätzlich)"
                    )
                else:
                    decision_titel = "Einschalten — Überschuss"
                    decision_reason = (
                        f"Überschuss ausreichend ({remaining_surplus:.2f} kW verfügbar, "
                        f"{predicted_power:.2f} kW benötigt)"
                    )
                await self._log_decision(dev, True, decision_titel, decision_reason)
            elif should_off:
                if soc_too_low:
                    decision_titel = "Ausschalten — Akku-Reserve unterschritten"
                    decision_reason = (
                        f"Überschuss reicht nicht ({remaining_surplus:.2f} kW verfügbar, "
                        f"{predicted_power:.2f} kW benötigt) und Akku-Reserve unterschritten "
                        f"(SOC {data.soc:.0f}% < {device_min_soc:.0f}%) — Akku darf für dieses "
                        f"Gerät nicht einspringen"
                    )
                else:
                    decision_titel = "Ausschalten — Überschuss/Akku reichen nicht"
                    decision_reason = (
                        f"Überschuss/Akku reichen nicht ({remaining_surplus:.2f} kW verfügbar, "
                        f"{predicted_power:.2f} kW benötigt, Akku würde nicht bis Solar-Start reichen)"
                    )
                await self._log_decision(dev, False, decision_titel, decision_reason)
            else:
                stable_titel = "Bleibt an — Hysterese" if is_on else "Bleibt aus — Hysterese"
                decision_reason = (
                    f"im Hysterese-Bereich ({remaining_surplus:.2f} kW Überschuss, "
                    f"{predicted_power:.2f} kW benötigt) — Zustand unverändert"
                )
                await self._log_decision(dev, is_on, stable_titel, decision_reason)

            if should_on and not blocked:
                # Reserve this device's predicted share (and its cutoff, if
                # any) so lower-priority devices only see what's genuinely
                # left over, and only for as long as this device actually
                # keeps drawing it. Skipped while blocked — a device
                # that's only pre-charging isn't actually drawing anything
                # yet, so it has nothing to reserve.
                cumulative_committed += predicted_power
                committed_segments.append((predicted_power, own_cutoff))

            if should_on and not is_on:
                # Capped rather than incremented without bound: once
                # pre-charged to the full hold while blocked, there's
                # nothing more to gain from counting further cycles, and an
                # uncapped counter would just be an ever-growing number
                # that means the same thing as STABLE_ON_CYCLES already
                # did.
                tracker.on_counter = min(tracker.on_counter + 1, STABLE_ON_CYCLES)
                tracker.off_counter = 0
                if tracker.on_counter >= STABLE_ON_CYCLES and not blocked:
                    _LOGGER.info(
                        "PV Surplus: turning ON %s (remaining_surplus=%.2f, need=%.2f, "
                        "battery_would_last=%s, force_runtime=%s)",
                        dev.get(CONF_DEVICE_NAME), remaining_surplus, predicted_power,
                        battery_would_last, force_runtime,
                    )
                    await async_turn_on(self.hass, dev)
                    tracker.on_counter = 0
            elif should_off and is_on:
                tracker.off_counter += 1
                tracker.on_counter = 0
                if tracker.off_counter >= required_off_cycles:
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (remaining_surplus=%.2f, need=%.2f, "
                        "battery_would_last=%s, waited=%d cycles)",
                        dev.get(CONF_DEVICE_NAME), remaining_surplus, predicted_power,
                        battery_would_last, required_off_cycles,
                    )
                    await async_turn_off(self.hass, dev)
                    tracker.off_counter = 0
            else:
                tracker.on_counter = 0
                tracker.off_counter = 0

            diag.off_counter = tracker.off_counter
            diag.on_counter = tracker.on_counter

        data.device_states = device_states
        data.device_diagnostics = device_diagnostics

        # Replace the naive avail_kwh / current_discharge_rate estimate set
        # in _async_update_data with the same time-window-aware projection
        # the cascade itself just used — committed_segments is exactly the
        # set of devices (and their cutoffs) the should_on/should_off
        # decisions above are based on. Otherwise the displayed "Akku
        # reicht" number and batt_ok/Modus would keep looking like a
        # shortfall right up until a windowed device's cutoff, even while
        # the real per-device logic already accounts for it and is fine.
        data.h_battery = self._hours_until_depleted(
            committed_segments, now_dt, data.avail_kwh, base_discharge_kw, available_surplus
        )
        data.batt_ok = (
            data.h_battery > (data.effective_h_to_solar + BATT_OK_BUFFER_H) and data.soc > data.min_soc
        )
