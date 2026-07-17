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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BATT_OK_BUFFER_H,
    CONF_BATT_SENSOR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_MIN_DAILY_RUNTIME_H,
    CONF_DEVICE_NAME,
    CONF_DEVICE_OFF_ONLY,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SCHEDULE_ENTITY,
    CONF_DEVICE_SWITCH,
    CONF_DEVICE_WINDOW_END,
    CONF_DEVICE_WINDOW_START,
    CONF_LOAD_SENSOR,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    CONF_SOLAR_OFFSETS,
    CONF_SOLAR_SENSOR,
    DEFAULT_SOLAR_OFFSETS,
    DISCHARGE_SMOOTHING_SAMPLES,
    DOMAIN,
    MARGIN_FOR_MAX_PATIENCE_H,
    MIN_RUNTIME_FORCE_AFTER_HOUR,
    MIN_SAMPLES_FOR_MEASURED_AVG,
    STABLE_OFF_CYCLES,
    STABLE_OFF_CYCLES_MAX,
    STABLE_ON_CYCLES,
    SURPLUS_OFF_THRESHOLD,
    SURPLUS_ON_THRESHOLD,
    UPDATE_INTERVAL_SECONDS,
)
from .power_tracker import DevicePowerTracker
from .runtime_tracker import DailyRuntimeTracker

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
    runtime_hours_today: float = 0.0
    force_runtime: bool = False


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
    h_battery: float = 999.0
    h_to_solar: float = 0.0
    solar_start: datetime | None = None
    batt_ok: bool = False
    min_soc: float = 20.0
    device_states: dict[str, bool] = field(default_factory=dict)
    device_diagnostics: dict[str, DeviceDiagnostics] = field(default_factory=dict)


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
        for dev in config.get(CONF_DEVICES, []):
            device_id = dev["_id"]
            self._device_trackers[device_id] = DeviceState(device_id=device_id)

    async def async_setup_power_trackers(self) -> None:
        """Load persisted per-device state: power samples (only if a power
        sensor is configured) and today's accumulated runtime (always, so
        the minimum daily runtime feature has history even if it's enabled
        later)."""
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

    @staticmethod
    def _required_off_cycles(data: CoordinatorData) -> int:
        """More battery margin beyond what's needed until solar resumes ->
        wait longer before reacting to a deficit, since it's more likely a
        short-lived spike than a real trend. No margin -> react fast."""
        margin_h = max(min(data.h_battery, 999.0) - data.h_to_solar, 0.0)
        fraction = min(margin_h / MARGIN_FOR_MAX_PATIENCE_H, 1.0)
        extra = (STABLE_OFF_CYCLES_MAX - STABLE_OFF_CYCLES) * fraction
        return round(STABLE_OFF_CYCLES + extra)

    def _get_solar_start(self) -> datetime:
        sun = self.hass.states.get("sun.sun")
        offsets = self._config.get(CONF_SOLAR_OFFSETS, DEFAULT_SOLAR_OFFSETS)
        m = dt_util.now().month
        offset_h = offsets[m - 1]

        if sun is None:
            return dt_util.utcnow() + timedelta(hours=12)

        next_rising_str = sun.attributes.get("next_rising")
        if not next_rising_str:
            return dt_util.utcnow() + timedelta(hours=12)

        next_rising = dt_util.parse_datetime(next_rising_str)
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

        batt_ok = h_battery > (h_to_solar + BATT_OK_BUFFER_H) and soc > min_soc

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
            solar_start=solar_start,
            batt_ok=batt_ok,
            min_soc=min_soc,
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
        wallbox_devices = [d for d in all_devices if d.get(CONF_DEVICE_IS_WALLBOX, False)]
        candidate_devices = [d for d in all_devices if not d.get(CONF_DEVICE_IS_WALLBOX, False)]

        wallbox_power_kw = sum(
            self._get_power_kw(wb.get(CONF_DEVICE_POWER_SENSOR)) for wb in wallbox_devices
        )

        # Figure out which candidate devices are currently on, and how much
        # power they're drawing right now, so we can subtract that from the
        # house load and recover the "base load" our devices don't control.
        device_is_on: dict[str, bool] = {}
        managed_power_kw = 0.0
        for dev in candidate_devices:
            device_id = dev["_id"]
            switch_id = dev.get(CONF_DEVICE_SWITCH)
            sw_state = self.hass.states.get(switch_id) if switch_id else None
            is_on = sw_state is not None and sw_state.state == "on"
            device_is_on[device_id] = is_on
            if is_on:
                sensor_id = dev.get(CONF_DEVICE_POWER_SENSOR)
                managed_power_kw += (
                    self._get_power_kw(sensor_id) if sensor_id else dev.get(CONF_DEVICE_POWER_KW, 0.15)
                )

        base_load = max(data.load_kw - wallbox_power_kw - managed_power_kw, 0.0)
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
        managed_discharge_kw = max(managed_power_kw - max(available_surplus, 0.0), 0.0)
        base_discharge_kw = max(data.smoothed_discharge_kw - managed_discharge_kw, 0.0)

        device_states: dict[str, bool] = {}
        device_diagnostics: dict[str, DeviceDiagnostics] = {}
        cumulative_committed = 0.0

        for dev in candidate_devices:
            device_id = dev["_id"]
            switch_id = dev.get(CONF_DEVICE_SWITCH)
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
            device_diagnostics[device_id] = diag

            if not switch_id:
                # No switch configured (shouldn't happen for non-wallbox
                # devices — validated at config time), nothing to actuate.
                continue

            tracker = self._device_trackers.setdefault(
                device_id, DeviceState(device_id=device_id)
            )

            # A configured time window is a hard boundary: outside it, the
            # device may only ever be off, enforced immediately (no
            # hysteresis) — this isn't a surplus/battery judgement call,
            # it's "not allowed to run right now at all". A device with the
            # legacy off_only flag and no window behaves like a window
            # that's always closed, for backward compatibility.
            if in_window is False or (in_window is None and legacy_off_only):
                tracker.on_counter = 0
                tracker.off_counter = 0
                if is_on:
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (outside its configured time window)",
                        dev.get(CONF_DEVICE_NAME),
                    )
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": switch_id}, blocking=False
                    )
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
            projected_committed = cumulative_committed + predicted_power
            uncovered_by_surplus = max(projected_committed - available_surplus, 0.0)
            projected_discharge = base_discharge_kw + uncovered_by_surplus
            projected_h_battery = (
                data.avail_kwh / projected_discharge if projected_discharge > 0.05 else 999.0
            )
            battery_would_last = (
                projected_h_battery > (data.h_to_solar + BATT_OK_BUFFER_H)
                and data.soc > data.min_soc
            )

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

            if should_on:
                # Reserve this device's predicted share so lower-priority
                # devices only see what's genuinely left over.
                cumulative_committed += predicted_power

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
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": switch_id}, blocking=False
                    )
                    tracker.on_counter = 0
            elif should_off and is_on:
                tracker.off_counter += 1
                tracker.on_counter = 0
                required_off_cycles = self._required_off_cycles(data)
                if tracker.off_counter >= required_off_cycles:
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (remaining_surplus=%.2f, need=%.2f, "
                        "battery_would_last=%s, waited=%d cycles)",
                        dev.get(CONF_DEVICE_NAME), remaining_surplus, predicted_power,
                        battery_would_last, required_off_cycles,
                    )
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": switch_id}, blocking=False
                    )
                    tracker.off_counter = 0
            else:
                tracker.on_counter = 0
                tracker.off_counter = 0

        data.device_states = device_states
        data.device_diagnostics = device_diagnostics
