"""Tracks measured power draw per device over a rolling active-runtime window.

Samples are taken once per coordinator cycle while the device is switched ON.
This gives a realistic average consumption (e.g. a miner that throttles under
heat, or a heat pump that modulates) instead of relying on a single number
entered once during setup.
"""
from __future__ import annotations

from collections import deque

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    MAX_SAMPLES_PER_DEVICE,
    POWER_STORE_SAVE_DELAY,
    STORAGE_VERSION,
)


class DevicePowerTracker:
    """Rolling power sample buffer for a single managed device, covering the
    last MAX_SAMPLES_PER_DEVICE samples of *active* runtime (see const.py) —
    not a calendar-time window. Samples are only appended while the device
    is on, so the deque's own maxlen eviction is the entire windowing
    mechanism: an idle device (e.g. a pool heat pump sitting unused through
    several rainy days) simply isn't touched, and its existing samples stay
    valid until it runs again, rather than aging out on the calendar and
    forcing a fallback to the configured estimate right when they're next
    needed.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, device_key: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_power_{entry_id}_{device_key}"
        )
        # Each sample: [iso_timestamp, power_kw]
        self._samples: deque[list] = deque(maxlen=MAX_SAMPLES_PER_DEVICE)

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data and "samples" in data:
            # Constructing with maxlen already trims any stored surplus
            # (e.g. from a smaller MAX_SAMPLES_PER_DEVICE in a previous
            # version) down to the current window on load.
            self._samples = deque(data["samples"], maxlen=MAX_SAMPLES_PER_DEVICE)

    def add_sample(self, power_kw: float) -> None:
        self._samples.append([dt_util.utcnow().isoformat(), power_kw])
        self._store.async_delay_save(self._data_to_save, POWER_STORE_SAVE_DELAY)

    def _data_to_save(self) -> dict:
        return {"samples": list(self._samples)}

    @property
    def average_kw(self) -> float | None:
        if not self._samples:
            return None
        values = [s[1] for s in self._samples]
        return sum(values) / len(values)

    @property
    def sample_count(self) -> int:
        return len(self._samples)
