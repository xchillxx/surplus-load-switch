"""Learns typical Grundlast (base_load) by weekday, hour of day, and house
mode (e.g. Zuhause/Abwesend/Schlafen/Urlaub) — diagnostic only for now,
does not feed any switching decision.

Self-sampling from the coordinator's own cycles, not the recorder's
history: this install's Grundlast entity_id isn't known generically (see
base_load_floor.py for the same issue elsewhere), and more importantly
the recorder's raw state history (needed here to know which house mode
was active at each sample — long-term statistics only pre-aggregate a
single numeric sensor, they can't be cross-referenced against a second
entity's state) is retained for a much shorter window by default (~10
days observed on a real installation) than the trailing 4 weeks this is
meant to cover. Sampling directly from coordinator cycles as they happen
has no such retention ceiling — it just keeps growing from whenever this
first runs.

Each (weekday, hour, house mode) bucket keeps up to LOAD_PROFILE_
TRAILING_SAMPLES daily averages — since each weekday only recurs once a
week, that's also roughly how many weeks of history a bucket holds. A
bucket below LOAD_PROFILE_MIN_SAMPLES daily samples isn't trusted on its
own yet; effective_average() falls back to progressively coarser
groupings (hour+mode across all weekdays, then hour alone) so the
diagnostic sensor has *something* meaningful from day one instead of
nothing until 4 weeks have passed.
"""
from __future__ import annotations

import logging
from datetime import date

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    LOAD_PROFILE_MIN_SAMPLES,
    LOAD_PROFILE_TRAILING_SAMPLES,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _bucket_key(weekday: str, hour: int, mode: str) -> str:
    return f"{weekday}_{hour}_{mode}"


class WeekdayLoadProfileLearner:
    """Tracks, per (weekday, hour, house mode), a rolling window of daily
    average base_load readings."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_load_profile_{entry_id}"
        )
        # bucket_key -> list of up to LOAD_PROFILE_TRAILING_SAMPLES daily
        # averages, oldest first (so [-1] is the most recent day).
        self._buckets: dict[str, list[float]] = {}
        # Which (weekday, hour, mode, calendar date) the in-progress
        # accumulator below belongs to — finalized into _buckets the
        # moment any part of this changes (hour ticks over, mode changes,
        # or a new day starts).
        self._current_key: tuple[str, int, str, date] | None = None
        self._current_samples: list[float] = []
        self._dirty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._buckets = {
            k: list(v) for k, v in data.get("buckets", {}).items()
        }

    async def async_save_now(self) -> None:
        """Force an immediate write — see coordinator.PVSurplusCoordinator.
        async_flush_stores for why this matters on unload/reload."""
        self._finalize_current(force=True)
        if self._dirty:
            await self._store.async_save({"buckets": self._buckets})
            self._dirty = False

    def record(self, base_load_kw: float, mode: str | None) -> None:
        """Call once per coordinator cycle with the current base_load and
        house mode (None if the configured entity is unavailable — that
        cycle is simply not sampled, nothing is finalized or lost)."""
        if mode is None:
            return
        now = dt_util.now()
        key = (WEEKDAYS[now.weekday()], now.hour, mode, now.date())
        if key != self._current_key:
            self._finalize_current()
            self._current_key = key
            self._current_samples = []
        self._current_samples.append(base_load_kw)

    def _finalize_current(self, force: bool = False) -> None:
        if self._current_key is None or not self._current_samples:
            return
        weekday, hour, mode, _day = self._current_key
        avg = sum(self._current_samples) / len(self._current_samples)
        bucket_key = _bucket_key(weekday, hour, mode)
        history = self._buckets.setdefault(bucket_key, [])
        history.append(round(avg, 3))
        del history[:-LOAD_PROFILE_TRAILING_SAMPLES]
        self._dirty = True
        if not force:
            self._store.async_delay_save(lambda: {"buckets": self._buckets}, 60)

    def effective_average(self, weekday: str, hour: int, mode: str) -> tuple[float | None, str]:
        """The learned average for (weekday, hour, mode), falling back to
        a coarser grouping when there isn't enough data yet. Returns
        (value, source) — source is one of "genau" (exact bucket),
        "stunde+modus" (this hour, this mode, any weekday), "stunde" (this
        hour, any weekday/mode), or "keine daten" (value is None)."""
        exact = self._buckets.get(_bucket_key(weekday, hour, mode), [])
        if len(exact) >= LOAD_PROFILE_MIN_SAMPLES:
            return sum(exact) / len(exact), "genau"

        by_hour_mode: list[float] = []
        by_hour: list[float] = []
        for wd in WEEKDAYS:
            same_hour_mode = self._buckets.get(_bucket_key(wd, hour, mode))
            if same_hour_mode:
                by_hour_mode.extend(same_hour_mode)
        if len(by_hour_mode) >= LOAD_PROFILE_MIN_SAMPLES:
            return sum(by_hour_mode) / len(by_hour_mode), "stunde+modus"

        for key, values in self._buckets.items():
            # bucket_key format is "<weekday>_<hour>_<mode>" — the mode
            # itself may contain underscores, so only the first two parts
            # are meaningful for this comparison.
            parts = key.split("_", 2)
            if len(parts) == 3 and parts[1] == str(hour):
                by_hour.extend(values)
        if len(by_hour) >= LOAD_PROFILE_MIN_SAMPLES:
            return sum(by_hour) / len(by_hour), "stunde"

        return None, "keine daten"

    @property
    def diagnostics(self) -> dict:
        total_days = sum(len(v) for v in self._buckets.values())
        return {
            "erfasste_kombinationen": len(self._buckets),
            "gesamte_tages_messwerte": total_days,
            "profil": dict(self._buckets),
        }
