"""Core logic: evaluates PV surplus and manages device switching.

Devices are switched in priority order using a cascade: the highest-priority
device gets first claim on available surplus, the next device only sees what's
left over after that, and so on. Each device's power need is either measured
(7-day rolling average while it's ON, see power_tracker.py) or, until enough
samples exist, the configured estimate.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BATT_OK_BUFFER_H,
    CALIBRATION_INTERVAL_HOURS,
    CONF_BATT_SENSOR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICES,
    CONF_DEVICE_DEPENDS_ON,
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
    DAYTIME_PROJECTION_HORIZON_H,
    DEFAULT_SOLAR_OFFSETS,
    DISCHARGE_SMOOTHING_SAMPLES,
    DOMAIN,
    LOAD_SENSOR_STALENESS_GRACE,
    MARGIN_FOR_MAX_PATIENCE_H,
    MIN_RUNTIME_FORCE_AFTER_HOUR,
    MIN_SAMPLES_FOR_MEASURED_AVG,
    OFF_CYCLES_FLOOR,
    STABLE_OFF_CYCLES,
    STABLE_OFF_CYCLES_MAX,
    STABLE_ON_CYCLES,
    STAGGER_CYCLES_PER_PRIORITY_STEP,
    STALENESS_MIN_REFRESHES,
    SURPLUS_OFF_THRESHOLD,
    SURPLUS_ON_THRESHOLD,
    UPDATE_INTERVAL_SECONDS,
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
    runtime_hours_today: float = 0.0
    force_runtime: bool = False
    effective_cutoff: str | None = None
    should_be_on: bool = False


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
        self._stale_managed_power_kw: float | None = None
        self._stale_since: datetime | None = None
        self._last_seen_load_kw: float | None = None
        self._load_refresh_count: int = 0
        self._last_seen_discharge_kw: float | None = None
        self._discharge_refresh_count: int = 0
        self._calibrator = SolarOffsetCalibrator(hass, entry_id, config[CONF_SOLAR_SENSOR])
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

    @property
    def devices(self) -> list[dict]:
        return sorted(
            self._config.get(CONF_DEVICES, []),
            key=lambda d: d.get(CONF_DEVICE_PRIORITY, 99),
        )

    def _get_float(self, entity_id: str, default: float = 0.0) -> float:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return default
        try:
            return float(state.state)
        except ValueError:
            return default

    def _require_valid(self, entity_id: str) -> None:
        """Raise if a core sensor isn't currently readable.

        Without this, a sensor going "unknown" (e.g. a brief integration
        hiccup) would silently read as 0 via _get_float's default — 0 solar
        looks exactly like "no sun" to the cascade, and after the off-hold
        buffer expires, devices would actually be switched off because of a
        communication glitch, not a real drop in production. Raising
        UpdateFailed here instead makes the coordinator keep its last good
        data and skip evaluating devices entirely this cycle, so nothing
        gets switched based on a sensor that isn't actually reporting.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
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
        # expensive to repeat every 30s cycle. Independent of the live
        # sensor checks below since it only reads historical statistics.
        if self._calibrator.due_for_recalibration(timedelta(hours=CALIBRATION_INTERVAL_HOURS)):
            await self._calibrator.async_recalibrate()

        for sensor_key in (CONF_SOLAR_SENSOR, CONF_LOAD_SENSOR, CONF_SOC_SENSOR, CONF_BATT_SENSOR):
            self._require_valid(self._config[sensor_key])

        solar = self._get_float(self._config[CONF_SOLAR_SENSOR])
        load = self._get_float(self._config[CONF_LOAD_SENSOR])
        soc = self._get_float(self._config[CONF_SOC_SENSOR])
        batt = self._get_float(self._config[CONF_BATT_SENSOR])
        battery_kwh = self._config.get(CONF_BATTERY_CAPACITY_KWH, 13.8)
        min_soc = self._config.get(CONF_MIN_SOC, 20.0)

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
        base_discharge_kw = max(data.smoothed_discharge_kw - managed_discharge_kw, 0.0)

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
            depends_on_id = dev.get(CONF_DEVICE_DEPENDS_ON)
            dependency_met = depends_on_id is None or device_is_on.get(depends_on_id, False)
            diag.dependency_met = dependency_met
            device_diagnostics[device_id] = diag

            if not control_id:
                # No switch or climate entity configured (shouldn't happen
                # for non-wallbox devices — validated at config time),
                # nothing to actuate.
                continue

            tracker = self._device_trackers.setdefault(
                device_id, DeviceState(device_id=device_id)
            )

            # A configured time window, or an unmet prerequisite device, is
            # a hard boundary: the device may only ever be off, enforced
            # immediately (no hysteresis) — neither is a surplus/battery
            # judgement call, both mean "not allowed to run right now at
            # all". A device with the legacy off_only flag and no window
            # behaves like a window that's always closed, for backward
            # compatibility.
            if in_window is False or (in_window is None and legacy_off_only) or not dependency_met:
                tracker.on_counter = 0
                tracker.off_counter = 0
                diag.should_be_on = False
                if is_on:
                    reason = (
                        "its prerequisite device is off" if not dependency_met
                        else "outside its configured time window"
                    )
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (%s)", dev.get(CONF_DEVICE_NAME), reason,
                    )
                    await async_turn_off(self.hass, dev)
                continue

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
            # "stay as you are", not a deviation either way.
            diag.should_be_on = should_on if (should_on or should_off) else is_on

            if should_on:
                # Reserve this device's predicted share (and its cutoff, if
                # any) so lower-priority devices only see what's genuinely
                # left over, and only for as long as this device actually
                # keeps drawing it.
                cumulative_committed += predicted_power
                committed_segments.append((predicted_power, own_cutoff))

            if should_on and not is_on:
                tracker.on_counter += 1
                tracker.off_counter = 0
                if tracker.on_counter >= STABLE_ON_CYCLES:
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
