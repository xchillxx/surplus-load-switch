"""Learns a realistic floor for base_load, replacing a hard 0.0.

base_load = max(house_load - wallbox - managed_devices, 0.0) implicitly
assumes the household can genuinely draw 0 kW right now — physically
impossible (fridge, standby electronics, networking gear never stop
drawing something). In practice the 0.0 floor kicks in constantly,
because several managed devices have no real power sensor configured
and fall back to a static config *estimate* instead — whenever that
estimate briefly overshoots what the device is actually drawing at that
moment, the subtraction goes negative and gets floored to 0.0, making
base_discharge_kw (and the battery-runway projection built on it) look
more favorable than reality for that cycle.

The raw, unmanaged house-load sensor's own recent minimum is a much
more honest floor: it's a real physical measurement, not an estimate,
and by definition can never read below what the household actually
needs at its quietest moment. Re-derived periodically from the
recorder's own hourly statistics (already retained regardless of the
recorder's raw-history purge period), mirroring solar_calibration.py's
approach — no sample storage of its own needed.

Uses a low percentile of the hourly minimums, not the outright minimum
— confirmed on a real installation that a single glitched hour (an
implausible exact 0.0 amid an otherwise consistent ~0.35-0.4 kW band
across every other hour in the window) is enough to poison a pure
min(), instantly undoing the whole point of this floor. A household
that's genuinely lower for a single hour out of ~80+ is one thing; one
hour reading exactly zero while every neighbour reads a consistent real
value is a sensor blip, the same kind of brief cloud-polling glitch the
core-sensor grace period already tolerates elsewhere — just showing up
here as a valid-looking "0" instead of "unavailable".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BASE_LOAD_FLOOR_LOOKBACK_DAYS,
    BASE_LOAD_FLOOR_PERCENTILE,
    CALIBRATION_RETRY_INTERVAL,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


class BaseLoadFloorCalibrator:
    """Rolling minimum of the raw house-load sensor over the last
    BASE_LOAD_FLOOR_LOOKBACK_DAYS — used as base_load's floor instead of
    a hard 0.0. Falls back to 0.0 (today's prior behaviour) until enough
    recorder history exists, e.g. right after a fresh install."""

    def __init__(self, hass: HomeAssistant, entry_id: str, load_entity_id: str) -> None:
        self._hass = hass
        self._load_entity_id = load_entity_id
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_base_load_floor_{entry_id}"
        )
        self._floor_kw: float | None = None
        self._sample_count: int = 0
        self._last_calibrated: datetime | None = None
        self._last_query_empty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._floor_kw = data.get("floor_kw")
        self._sample_count = data.get("sample_count", 0)
        last = data.get("last_calibrated")
        self._last_calibrated = dt_util.parse_datetime(last) if last else None

    @property
    def floor_kw(self) -> float:
        """The learned floor, or 0.0 until enough history exists."""
        return self._floor_kw if self._floor_kw is not None else 0.0

    @property
    def diagnostics(self) -> dict:
        return {
            "floor_kw": self._floor_kw,
            "stundenwerte": self._sample_count,
            "zuletzt_kalibriert": self._last_calibrated.isoformat() if self._last_calibrated else None,
        }

    def due_for_recalibration(self, interval: timedelta) -> bool:
        if self._last_calibrated is None:
            return True
        effective_interval = CALIBRATION_RETRY_INTERVAL if self._last_query_empty else interval
        return dt_util.utcnow() - self._last_calibrated >= effective_interval

    async def async_recalibrate(self) -> None:
        """Pull the raw house-load sensor's hourly statistics and take
        the minimum over the last BASE_LOAD_FLOOR_LOOKBACK_DAYS. The
        recorder query is blocking, so it runs in the executor — safe to
        call from the event loop."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period
        except ImportError:
            _LOGGER.debug("Base load floor: recorder not available, skipping")
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=BASE_LOAD_FLOOR_LOOKBACK_DAYS)

        def _query() -> dict:
            return statistics_during_period(
                self._hass, start, end, {self._load_entity_id}, "hour", None, {"min"},
            )

        try:
            result = await get_instance(self._hass).async_add_executor_job(_query)
        except Exception:  # noqa: BLE001 — a calibration failure must never break switching
            _LOGGER.exception("Base load floor: failed to read statistics")
            return

        points = result.get(self._load_entity_id, [])
        mins = [p["min"] for p in points if p.get("min") is not None]
        if not mins:
            # No history yet (fresh install) or the recorder's statistics
            # index isn't ready right at startup — retry sooner than the
            # normal cadence rather than locking in "no floor" for a
            # full day.
            _LOGGER.debug(
                "Base load floor: no statistics returned for %s (queried %s to %s) "
                "— will retry sooner than the normal cadence",
                self._load_entity_id, start, end,
            )
            self._last_calibrated = dt_util.utcnow()
            self._last_query_empty = True
            return

        # Nearest-rank percentile: sort ascending and take the value at
        # index round(percentile/100 * (n-1)) — robust to a handful of
        # outlier-low hours instead of being fully determined by the
        # single lowest one.
        mins.sort()
        index = round(BASE_LOAD_FLOOR_PERCENTILE / 100 * (len(mins) - 1))
        self._floor_kw = round(mins[index], 3)
        self._sample_count = len(mins)
        self._last_calibrated = dt_util.utcnow()
        self._last_query_empty = False
        await self._store.async_save({
            "floor_kw": self._floor_kw,
            "sample_count": self._sample_count,
            "last_calibrated": self._last_calibrated.isoformat(),
        })
        _LOGGER.info(
            "Base load floor: recalibrated to %.3f kW (p%d) from %d hourly point(s) "
            "over the last %d day(s)",
            self._floor_kw, BASE_LOAD_FLOOR_PERCENTILE, self._sample_count,
            BASE_LOAD_FLOOR_LOOKBACK_DAYS,
        )
