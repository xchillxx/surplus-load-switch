"""Tracks a device's accumulated ON time for the current local day.

Used to guarantee a configured minimum daily runtime (e.g. a pool pump that
needs to filter for at least 4h/day) even on days with too little PV surplus
to reach it otherwise.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import RUNTIME_STORE_SAVE_DELAY, STORAGE_VERSION


class DailyRuntimeTracker:
    """Seconds a single device has been ON today (local calendar day)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device_key: str) -> None:
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_runtime_{entry_id}_{device_key}"
        )
        self._date: str = dt_util.now().date().isoformat()
        self._seconds_today: float = 0.0

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._date = data.get("date", self._date)
            self._seconds_today = data.get("seconds_today", 0.0)
        self._roll_over_if_new_day()

    def _roll_over_if_new_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if today != self._date:
            self._date = today
            self._seconds_today = 0.0

    def add_cycle(self, is_on: bool, cycle_seconds: float) -> None:
        self._roll_over_if_new_day()
        if is_on:
            self._seconds_today += cycle_seconds
        self._store.async_delay_save(self._data_to_save, RUNTIME_STORE_SAVE_DELAY)

    def _data_to_save(self) -> dict:
        return {"date": self._date, "seconds_today": self._seconds_today}

    @property
    def hours_today(self) -> float:
        self._roll_over_if_new_day()
        return self._seconds_today / 3600.0
