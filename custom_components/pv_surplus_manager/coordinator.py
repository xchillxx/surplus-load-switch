"""Core logic: evaluates PV surplus and manages device switching.

Devices are switched in priority order using a cascade: the highest-priority
device gets first claim on available surplus, the next device only sees what's
left over after that, and so on. Each device's power need is either measured
(7-day rolling average while it's ON, see power_tracker.py) or, until enough
samples exist, the configured estimate.
"""
from __future__ import annotations

import logging
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
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SWITCH,
    CONF_LOAD_SENSOR,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    CONF_SOLAR_OFFSETS,
    CONF_SOLAR_SENSOR,
    DEFAULT_SOLAR_OFFSETS,
    DISCHARGE_SMOOTHING_SAMPLES,
    DOMAIN,
    MIN_SAMPLES_FOR_MEASURED_AVG,
    STABLE_OFF_CYCLES,
    STABLE_ON_CYCLES,
    SURPLUS_OFF_THRESHOLD,
    SURPLUS_ON_THRESHOLD,
    UPDATE_INTERVAL_SECONDS,
)
from .power_tracker import DevicePowerTracker

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


@dataclass
class CoordinatorData:
    """Snapshot of all computed values, exposed to entities."""
    solar_kw: float = 0.0
    load_kw: float = 0.0
    soc: float = 0.0
    batt_kw: float = 0.0
    discharge_kw: float = 0.0
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
        self._discharge_samples: deque[float] = deque(maxlen=DISCHARGE_SMOOTHING_SAMPLES)
        for dev in config.get(CONF_DEVICES, []):
            device_id = dev["_id"]
            self._device_trackers[device_id] = DeviceState(device_id=device_id)

    async def async_setup_power_trackers(self) -> None:
        """Load persisted power samples for every device that has a power sensor."""
        for dev in self._config.get(CONF_DEVICES, []):
            sensor_id = dev.get(CONF_DEVICE_POWER_SENSOR)
            if not sensor_id:
                continue
            device_id = dev["_id"]
            tracker = DevicePowerTracker(self.hass, self._entry_id, device_id)
            await tracker.async_load()
            self._power_trackers[device_id] = tracker

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
        # h_battery is a division by discharge rate, which amplifies normal
        # sensor noise into large hour swings near the threshold. Smooth over
        # the last few cycles so a single noisy reading can't flip batt_ok.
        smoothed_discharge = sum(self._discharge_samples) / len(self._discharge_samples)
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

            predicted_power, diag = self._predicted_power_kw(dev)
            diag.is_on = is_on
            device_diagnostics[device_id] = diag

            if not switch_id:
                # No switch configured (shouldn't happen for non-wallbox
                # devices — validated at config time), nothing to actuate.
                continue

            remaining_surplus = available_surplus - cumulative_committed
            should_on = (remaining_surplus > predicted_power + SURPLUS_ON_THRESHOLD) or data.batt_ok
            should_off = (
                remaining_surplus < predicted_power + SURPLUS_OFF_THRESHOLD
            ) and not data.batt_ok

            if should_on:
                # Reserve this device's predicted share so lower-priority
                # devices only see what's genuinely left over.
                cumulative_committed += predicted_power

            tracker = self._device_trackers.setdefault(
                device_id, DeviceState(device_id=device_id)
            )

            if should_on and not is_on:
                tracker.on_counter += 1
                tracker.off_counter = 0
                if tracker.on_counter >= STABLE_ON_CYCLES:
                    _LOGGER.info(
                        "PV Surplus: turning ON %s (remaining_surplus=%.2f, need=%.2f, batt_ok=%s)",
                        dev.get(CONF_DEVICE_NAME), remaining_surplus, predicted_power, data.batt_ok,
                    )
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": switch_id}, blocking=False
                    )
                    tracker.on_counter = 0
            elif should_off and is_on:
                tracker.off_counter += 1
                tracker.on_counter = 0
                if tracker.off_counter >= STABLE_OFF_CYCLES:
                    _LOGGER.info(
                        "PV Surplus: turning OFF %s (remaining_surplus=%.2f, need=%.2f, batt_ok=%s)",
                        dev.get(CONF_DEVICE_NAME), remaining_surplus, predicted_power, data.batt_ok,
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
