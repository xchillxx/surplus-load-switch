"""Tracks measured power draw per device over a rolling 7-day window.

Samples are taken once per coordinator cycle while the device is switched ON.
This gives a realistic average consumption (e.g. a miner that throttles under
heat, or a heat pump that modulates) instead of relying on a single number
entered once during setup.
"""
from __future__ import annotations

from collections import deque
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    MAX_SAMPLES_PER_DEVICE,
    POWER_HISTORY_WINDOW_DAYS,
    POWER_STORE_SAVE_DELAY,
    STORAGE_VERSION,
)


class DevicePowerTracker:
    """Rolling 7-day power sample buffer for a single managed device."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device_key: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"pv_surplus_manager_power_{entry_id}_{device_key}"
        )
        # Each sample: [iso_timestamp, power_kw]
        self._samples: deque[list] = deque(maxlen=MAX_SAMPLES_PER_DEVICE)

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data and "samples" in data:
            self._samples = deque(data["samples"], maxlen=MAX_SAMPLES_PER_DEVICE)
        self._prune()

    def add_sample(self, power_kw: float) -> None:
        self._samples.append([dt_util.utcnow().isoformat(), power_kw])
        self._prune()
        self._store.async_delay_save(self._data_to_save, POWER_STORE_SAVE_DELAY)

    def _prune(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=POWER_HISTORY_WINDOW_DAYS)
        while self._samples:
            ts = dt_util.parse_datetime(self._samples[0][0])
            if ts is not None and ts < cutoff:
                self._samples.popleft()
            else:
                break

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
