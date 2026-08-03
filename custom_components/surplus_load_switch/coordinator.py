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
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BATT_OK_BUFFER_H,
    CALIBRATION_INTERVAL_HOURS,
    CONF_BATT_SENSOR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICES,
    CONF_DEVICE_DEPENDS_ON,
    CONF_DEVICE_ENABLED,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_MIN_DAILY_RUNTIME_H,
    CONF_DEVICE_NAME,
    CONF_DEVICE_OFF_ONLY,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SCHEDULE_ENTITY,
    CONF_DEVICE_WINDOW_END,
    CONF_DEVICE_WINDOW_START,
    CONF_LOAD_SENSOR,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    CONF_SOLAR_OFFSETS,
    CONF_SOLAR_SENSOR,
    CONF_WALLBOX_SATISFIED_KW,
    CONF_WALLBOX_WEAK_DAY_PRIORITY,
    CORE_SENSOR_GRACE_PERIOD,
    DAYTIME_PROJECTION_HORIZON_H,
    DEFAULT_SOLAR_OFFSETS,
    DISCHARGE_SMOOTHING_SAMPLES,
    DOMAIN,
    LOAD_SENSOR_STALENESS_GRACE,
    MARGIN_FOR_MAX_PATIENCE_H,
    MIN_RUNTIME_FORCE_AFTER_HOUR,
    MIN_SAMPLES_FOR_MEASURED_AVG,
    OFF_CYCLES_FLOOR,
    POWER_STORE_SAVE_DELAY,
    STABLE_OFF_CYCLES,
    STABLE_OFF_CYCLES_MAX,
    STABLE_ON_CYCLES,
    STAGGER_CYCLES_PER_PRIORITY_STEP,
    STALENESS_MIN_REFRESHES,
    SOLAR_START_MIN_KW,
    STORAGE_VERSION,
    SURPLUS_OFF_THRESHOLD,
    SURPLUS_ON_THRESHOLD,
    UPDATE_INTERVAL_SECONDS,
    WALLBOX_IDLE_THRESHOLD_KW,
    WEAK_DAY_BATTERY_FULL_SOC,
    WEAK_DAY_EARLIEST_CHECK_HOUR,
    WEAK_DAY_RATIO_THRESHOLD,
)
from .device_control import async_turn_off, async_turn_on, control_entity_id, is_device_on
from .power_tracker import DevicePowerTracker
from .runtime_tracker import DailyRuntimeTracker
from .solar_calibration import SolarOffsetCalibrator

_LOGGER = logging.getLogger(__name__)


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
    active_solar_offset_h: float = 0.0
    next_cycle_at: datetime | None = None
    soc_gain_today: float | None = None
    peak_soc_gain_today: float = 0.0
    reference_soc_gain: float | None = None
    is_weak_day: bool = False


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
        # Which managed devices were on as of the last cycle — used to
        # detect a composition change and reset the discharge smoothing
        # window when one happens (see _evaluate_devices).
        self._last_managed_on: frozenset[str] = frozenset()
        # Tracks the managed-device mix a still-unrefreshed load/discharge
        # reading was last known to actually reflect, and the on/off
        # composition as of the previous cycle — see the staleness
        # correction in _evaluate_devices. The freeze releases once both
        # source sensors have each genuinely refreshed at least
        # STALENESS_MIN_REFRESHES times since the transition (real
        # evidence they've caught up), not just after a fixed delay —
        # confirmed against real data that a fixed timer can release right
        # as the sensor is mid-refresh, before its value has actually
        # settled, capped by LOAD_SENSOR_STALENESS_GRACE so a stalled
        # sensor doesn't freeze this indefinitely.
        self._last_managed_power_kw: float = 0.0
        self._managed_power_kw_seen: bool = False
        self._stale_managed_power_kw: float | None = None
        self._stale_since: datetime | None = None
        self._last_seen_load_kw: float | None = None
        self._load_refresh_count: int = 0
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
        # Battery SOC captured the moment solar production starts today
        # (see SOLAR_START_MIN_KW), for weak-day detection — reset to None
        # whenever the local calendar date changes, then set exactly once
        # for the rest of that day.
        self._today_date: date | None = None
        self._today_solar_start_soc: float | None = None
        # Highest SOC gain (and highest raw SOC) seen so far today — weak-
        # day status is decided from these peaks, not the live/current
        # values, so it can't flap back to "weak" once disproven just
        # because the battery is discharging again in the evening (SOC
        # gain, unlike the old peak-solar-kW metric, isn't monotonic
        # within a day on its own). Reset alongside _today_solar_start_soc.
        self._today_peak_soc_gain: float = 0.0
        self._today_peak_soc: float = 0.0
        # Persists the four values above across restarts/reloads — in-
        # memory-only tracking meant "already proved itself today" got
        # silently forgotten by every restart during that same day (e.g. a
        # round of updates), re-exposing devices to a weak-day block a
        # battery that actually topped up hours earlier no longer
        # deserved. Loaded once via async_load_daily_state() (called from
        # __init__.py before the first refresh); only trusted if the
        # stored date is still today, so a restart on a genuinely new day
        # starts fresh exactly like before.
        self._daily_state_store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_daily_{entry_id}"
        )
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
        self._calibrator = SolarOffsetCalibrator(
            hass, entry_id, config[CONF_SOLAR_SENSOR], config[CONF_SOC_SENSOR]
        )
        self._last_offset_h = 0.0
        for dev in config.get(CONF_DEVICES, []):
            device_id = dev["_id"]
            self._device_trackers[device_id] = DeviceState(device_id=device_id)

    async def async_setup_power_trackers(self) -> None:
        """Load persisted per-device state: power samples (only if a power
        sensor is configured) and today's accumulated runtime (always, so
        the minimum daily runtime feature has history even if it's enabled
        later). Also loads the last computed solar-offset calibration and
        today's weak-day peak-tracking state."""
        await self._calibrator.async_load()
        await self._async_load_daily_state()
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

    async def _async_load_daily_state(self) -> None:
        data = await self._daily_state_store.async_load()
        if not data:
            return
        stored_date = data.get("date")
        if stored_date != dt_util.now().date().isoformat():
            # Stale from a previous day — today starts fresh exactly like
            # a first-ever run would, via the normal date-change reset in
            # _async_update_data.
            return
        self._today_date = dt_util.now().date()
        self._today_solar_start_soc = data.get("solar_start_soc")
        self._today_peak_soc_gain = data.get("peak_soc_gain", 0.0)
        self._today_peak_soc = data.get("peak_soc", 0.0)

    def _daily_state_to_save(self) -> dict:
        return {
            "date": self._today_date.isoformat(),
            "solar_start_soc": self._today_solar_start_soc,
            "peak_soc_gain": self._today_peak_soc_gain,
            "peak_soc": self._today_peak_soc,
        }

    def _save_daily_state(self) -> None:
        if self._today_date is None:
            return
        self._daily_state_store.async_delay_save(
            self._daily_state_to_save,
            POWER_STORE_SAVE_DELAY,
        )

    async def async_flush_stores(self) -> None:
        """Force-write every debounced Store immediately — runtime
        trackers, power trackers, and the daily-state (weak-day peak)
        store all re-trigger their own debounce timer on every single
        coordinator cycle while active, which can leave them without a
        quiet window to actually flush to disk on their own for an entire
        day (see DailyRuntimeTracker.async_save_now). Call this right
        before the integration unloads — every version update reloads it —
        so today's tracking survives instead of reverting to a stale disk
        copy on the next load.
        """
        for tracker in self._runtime_trackers.values():
            await tracker.async_save_now()
        for tracker in self._power_trackers.values():
            await tracker.async_save_now()
        if self._today_date is not None:
            await self._daily_state_store.async_save(self._daily_state_to_save())

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

        Three independent sources, the earliest of which wins since any
        one of them alone forces the device off:
        - A schedule.* helper's own `next_event` attribute while the
          schedule is currently "on" — next_event is then necessarily the
          moment it turns off.
        - A simple window_end time (next occurrence from now, including
          past-midnight wraparound).
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

        schedule_entity = dev.get(CONF_DEVICE_SCHEDULE_ENTITY)
        if schedule_entity:
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
                candidate = dt_util.now().replace(
                    hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0
                )
                candidate_utc = dt_util.as_utc(candidate)
                if candidate_utc <= now:
                    candidate_utc += timedelta(days=1)
                candidates.append(candidate_utc)

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

        solar = self._get_core_float(self._config[CONF_SOLAR_SENSOR])
        load = self._get_core_float(self._config[CONF_LOAD_SENSOR])
        soc = self._get_core_float(self._config[CONF_SOC_SENSOR])
        batt = self._get_core_float(self._config[CONF_BATT_SENSOR])
        battery_kwh = self._config.get(CONF_BATTERY_CAPACITY_KWH, 13.8)
        min_soc = self._config.get(CONF_MIN_SOC, 20.0)

        # Weak-day detection: capture the battery's SOC the moment solar
        # production starts today (see SOLAR_START_MIN_KW), then compare
        # how much it's gained since against the calibrated "normal" gain
        # for this month, once it's late enough that a genuinely strong
        # morning would already have shown it (see
        # WEAK_DAY_EARLIEST_CHECK_HOUR — before that, a low gain-so-far
        # just means the sun hasn't gotten going yet, not that today is
        # weak). SOC gain (not raw solar power) is used so a brief sun
        # break through passing clouds doesn't swing the reading, and so
        # household consumption along the way is naturally accounted for.
        local_now = dt_util.now()
        if self._today_date != local_now.date():
            self._today_date = local_now.date()
            self._today_solar_start_soc = None
            self._today_peak_soc_gain = 0.0
            self._today_peak_soc = 0.0
        if self._today_solar_start_soc is None and solar >= SOLAR_START_MIN_KW:
            self._today_solar_start_soc = soc
        soc_gain_today = (
            soc - self._today_solar_start_soc
            if self._today_solar_start_soc is not None else None
        )
        self._today_peak_soc = max(self._today_peak_soc, soc)
        if soc_gain_today is not None:
            self._today_peak_soc_gain = max(self._today_peak_soc_gain, soc_gain_today)
        self._save_daily_state()
        reference_soc_gain = self._calibrator.effective_reference_soc_gain(local_now.month)
        # Decided from today's *peak* gain/SOC, not the live values — SOC
        # gain naturally falls again once the battery starts discharging
        # in the evening, and a day that already proved itself strong
        # (or the battery topped up) earlier on shouldn't flip back to
        # "weak" just because it's evening now.
        is_weak_day = (
            reference_soc_gain is not None
            and reference_soc_gain > 0
            and local_now.hour >= WEAK_DAY_EARLIEST_CHECK_HOUR
            and self._today_peak_soc_gain < WEAK_DAY_RATIO_THRESHOLD * reference_soc_gain
        )

        discharge = max(-batt, 0.0)
        self._discharge_samples.append(discharge)
        # h_battery is a division by discharge rate, which would otherwise
        # project a brief spike (e.g. a stove running for 10-15 min) forward
        # as if it continued all night. The median over a 20 min window
        # ignores such a spike almost entirely while still tracking a real,
        # sustained change in load within roughly half the window's length.
        smoothed_discharge = statistics.median(self._discharge_samples)
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
        effective_h_to_solar = DAYTIME_PROJECTION_HORIZON_H if sun_above_horizon else h_to_solar

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
            active_solar_offset_h=self._last_offset_h,
            next_cycle_at=dt_util.utcnow() + timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            soc_gain_today=soc_gain_today,
            peak_soc_gain_today=self._today_peak_soc_gain,
            reference_soc_gain=reference_soc_gain,
            is_weak_day=is_weak_day,
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
        # composition change until BOTH the load and discharge sensors
        # have each produced at least STALENESS_MIN_REFRESHES genuinely
        # new readings since — real evidence they've cycled past the
        # transition, rather than guessing a fixed delay — capped by
        # LOAD_SENSOR_STALENESS_GRACE in case a sensor stalls and never
        # reaches that count.
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

        if managed_power_kw != self._last_managed_power_kw and self._stale_managed_power_kw is None:
            # Only capture a fresh freeze point if we're not already mid
            # grace-period — a second device changing before the sensors
            # have caught up with the first (e.g. the pool pump and its
            # dependent heat pump both hitting their cutoff within the
            # same minute) must not overwrite the original pre-cluster
            # value with an intermediate one the sensors never actually
            # reflected either.
            self._stale_managed_power_kw = self._last_managed_power_kw
            self._stale_since = now
            self._last_seen_load_kw = data.load_kw
            self._load_refresh_count = 0
            self._last_seen_discharge_kw = data.discharge_kw
            self._discharge_refresh_count = 0

        effective_managed_power_kw = managed_power_kw
        if self._stale_managed_power_kw is not None:
            if data.load_kw != self._last_seen_load_kw:
                self._load_refresh_count += 1
                self._last_seen_load_kw = data.load_kw
            if data.discharge_kw != self._last_seen_discharge_kw:
                self._discharge_refresh_count += 1
                self._last_seen_discharge_kw = data.discharge_kw

            caught_up = (
                self._load_refresh_count >= STALENESS_MIN_REFRESHES
                and self._discharge_refresh_count >= STALENESS_MIN_REFRESHES
            )
            timed_out = now - self._stale_since >= LOAD_SENSOR_STALENESS_GRACE
            if caught_up or timed_out:
                self._stale_managed_power_kw = None
                self._stale_since = None
            else:
                effective_managed_power_kw = self._stale_managed_power_kw

        self._last_managed_power_kw = managed_power_kw

        base_load = max(data.load_kw - wallbox_power_kw - effective_managed_power_kw, 0.0)
        available_surplus = data.solar_kw - base_load

        data.base_load_kw = base_load
        data.surplus_kw = available_surplus

        # Computed once per cycle, not per dependent device — several
        # devices could depend on the same wallbox, and calling
        # _wallbox_satisfied once per dependent would advance its idle
        # hold-time counter faster than once per cycle.
        wallbox_satisfied = {
            wb["_id"]: self._wallbox_satisfied(wb) for wb in wallbox_devices
        }

        # The most protective (lowest/most-important) configured weak-day
        # priority across all wallboxes — a candidate device worse than
        # this gets held off entirely on a weak day (see the per-device
        # loop below). None if no wallbox has this set.
        wallbox_weak_day_priorities = [
            wb[CONF_WALLBOX_WEAK_DAY_PRIORITY] for wb in wallbox_devices
            if wb.get(CONF_WALLBOX_WEAK_DAY_PRIORITY)
        ]
        weak_day_priority_threshold = (
            min(wallbox_weak_day_priorities) if wallbox_weak_day_priorities else None
        )
        # Once every wallbox actually protecting a weak-day priority is
        # satisfied (car full or gone — the same idle/threshold detection
        # _wallbox_satisfied already uses for the dependency feature),
        # there's nothing left to hold surplus back for: letting other
        # devices use it beats it going unused to the grid. Vacuously
        # True (but never consulted) when no wallbox has a weak-day
        # priority configured at all.
        weak_day_wallboxes_satisfied = all(
            wallbox_satisfied.get(wb["_id"], True)
            for wb in wallbox_devices
            if wb.get(CONF_WALLBOX_WEAK_DAY_PRIORITY)
        )

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
        # This must use effective_managed_power_kw, the same
        # staleness-corrected figure base_load uses above, not the fresh
        # managed_power_kw — confirmed directly against real data that the
        # battery charge/discharge sensor lags on the same ~5-minute cloud
        # polling cadence as the load sensor (both come from the same
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
        managed_discharge_kw = max(effective_managed_power_kw - max(available_surplus, 0.0), 0.0)
        if data.sun_above_horizon:
            base_discharge_kw = max(data.smoothed_discharge_kw - managed_discharge_kw, 0.0)
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
            # already on its way out.
            base_discharge_kw = base_load

        device_states: dict[str, bool] = {}
        device_diagnostics: dict[str, DeviceDiagnostics] = {}
        cumulative_committed = 0.0
        committed_segments: list[tuple[float, datetime | None]] = []
        now_dt = dt_util.utcnow()
        horizon_end = now_dt + timedelta(hours=data.effective_h_to_solar + BATT_OK_BUFFER_H)

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
            force_runtime = (
                min_daily_runtime_h is not None
                and runtime_hours_today < min_daily_runtime_h
                and dt_util.now().hour >= MIN_RUNTIME_FORCE_AFTER_HOUR
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

            # On a detected weak day (see _async_update_data), a device at
            # or worse than a wallbox's configured weak-day priority is
            # held back the same hard way — the wallbox effectively takes
            # over that priority slot for the day (a device configured
            # with the *same* priority number as the wallbox's weak-day
            # value counts as behind it, not tied with it), pushing
            # everything from there on down behind it. No point spending
            # scarce surplus on low-priority devices while today's
            # production is running well below normal for the season,
            # unless the battery's already basically full anyway (then
            # there's nothing left to protect it for) — checked against
            # today's *peak* SOC, not the live value, so a battery that
            # topped up earlier and is now discharging in the evening
            # doesn't reopen the block it already earned its way out of.
            # Also only while the sun's actually up: the whole point is
            # protecting surplus for the wallbox, and overnight there's no
            # surplus at all for it to compete over — holding a device
            # back after dark wouldn't save anything for the car, only
            # cost the device a night's runtime for nothing. And only
            # while the wallbox actually still needs it: once it's
            # satisfied (car full or gone), holding other devices back
            # any longer wouldn't protect anything — the surplus would
            # just go unused to the grid instead of being used here.
            weak_day_block = (
                data.is_weak_day
                and weak_day_priority_threshold is not None
                and diag.priority >= weak_day_priority_threshold
                and self._today_peak_soc < WEAK_DAY_BATTERY_FULL_SOC
                and data.sun_above_horizon
                and not weak_day_wallboxes_satisfied
            )

            # A detected weak day is a hard boundary with no known future
            # "opens at" moment — no point pre-charging the on-hold for
            # it, so it still resets immediately and skips the rest of
            # the evaluation entirely. A device with the legacy off_only
            # flag and no window behaves like a window that's always
            # closed, same as before.
            if weak_day_block or (in_window is None and legacy_off_only):
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                hard_off_reason = (
                    f"schwacher Tag (bester Akku-Zuwachs heute +{data.peak_soc_gain_today:.1f}% "
                    f"von normal +{data.reference_soc_gain:.1f}%, Akku heute nie über "
                    f"{WEAK_DAY_BATTERY_FULL_SOC:.0f}%)"
                )
                await self._log_decision(dev, False, "Schwacher Tag", hard_off_reason)
                if is_on:
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (%s)", dev.get(CONF_DEVICE_NAME), hard_off_reason,
                    )
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
            # boundary like weak_day_block above — no point pre-charging
            # on_counter for a reopening hours away. Without this, a device
            # sits primed (and shows a misleading "wartet noch X min bis
            # einschalten" countdown) for the rest of the evening right
            # after every window close, even though it can't actually turn
            # on until blocked clears regardless.
            if window_far_closed and not is_on:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                await self._log_decision(
                    dev, False, "Außerhalb Zeitfenster",
                    "außerhalb des Zeitfensters — öffnet erst wieder später, kein Vorladen",
                )
                continue

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
            # see the two `not blocked` guards below. A weak-day block
            # doesn't get this treatment (see above) since it has no
            # equivalent discrete "clears at" moment worth pre-charging
            # for.

            remaining_surplus = available_surplus - cumulative_committed

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
            diag.effective_cutoff = own_cutoff.isoformat() if own_cutoff else None
            projected_segments = [*committed_segments, (predicted_power, own_cutoff)]
            energy_needed_kwh = self._project_energy_kwh(
                projected_segments, now_dt, horizon_end, base_discharge_kw, available_surplus
            )
            battery_would_last = data.avail_kwh > energy_needed_kwh and data.soc > data.min_soc
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
                    f"benötigt) — wartet noch auf {wartet_auf}"
                )
                titel = "Vorbereitet — wartet auf Zeitfenster" if window_closed else "Vorbereitet — wartet auf Abhängigkeit"
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
                decision_reason = (
                    f"Überschuss/Akku reichen nicht ({remaining_surplus:.2f} kW verfügbar, "
                    f"{predicted_power:.2f} kW benötigt, Akku würde nicht bis Solar-Start reichen)"
                )
                await self._log_decision(dev, False, "Ausschalten — Überschuss/Akku reichen nicht", decision_reason)
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
