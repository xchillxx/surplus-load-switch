"""Learns a wallbox's realistic maximum charge rate from its own history.

CONF_WALLBOX_MAX_CHARGE_KW (see coordinator._wallbox_reserved_kw) caps
the dynamic surplus reservation at whatever the car/charger can actually
draw — no point reserving more than that regardless of how large the
remaining deficit is. Entering that number by hand works, but goes stale
the moment reality changes (3-phase summer charging vs. a single-phase
winter fallback, an amperage limit change, a different car) — nobody
remembers to come back and update a config number for that.

The wallbox's own power sensor already knows the answer: its rolling
maximum over the last WALLBOX_MAX_CHARGE_LOOKBACK_DAYS. Re-derived
periodically from the recorder's own hourly statistics (already retained
regardless of the recorder's raw-history purge period), mirroring
base_load_floor.py's approach — no sample storage of its own needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CALIBRATION_RETRY_INTERVAL,
    STORAGE_VERSION,
    WALLBOX_MAX_CHARGE_LOOKBACK_DAYS,
)

_LOGGER = logging.getLogger(__name__)


class WallboxChargeCalibrator:
    """Rolling maximum of a wallbox's own power sensor over the last
    WALLBOX_MAX_CHARGE_LOOKBACK_DAYS — used as the learned fallback for
    CONF_WALLBOX_MAX_CHARGE_KW when that field is left unset. None
    (no cap beyond whatever surplus exists) until enough recorder
    history exists, e.g. right after a fresh install."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device_id: str, power_entity_id: str) -> None:
        self._hass = hass
        self._power_entity_id = power_entity_id
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"surplus_load_switch_wallbox_max_charge_{entry_id}_{device_id}"
        )
        self._max_charge_kw: float | None = None
        self._sample_count: int = 0
        self._last_calibrated: datetime | None = None
        self._last_query_empty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._max_charge_kw = data.get("max_charge_kw")
        self._sample_count = data.get("sample_count", 0)
        last = data.get("last_calibrated")
        self._last_calibrated = dt_util.parse_datetime(last) if last else None

    @property
    def max_charge_kw(self) -> float | None:
        """The learned maximum, or None until enough history exists —
        callers should treat None the same as "no cap", not 0.0."""
        return self._max_charge_kw

    @property
    def diagnostics(self) -> dict:
        return {
            "max_charge_kw": self._max_charge_kw,
            "stundenwerte": self._sample_count,
            "zuletzt_kalibriert": self._last_calibrated.isoformat() if self._last_calibrated else None,
        }

    def due_for_recalibration(self, interval: timedelta) -> bool:
        if self._last_calibrated is None:
            return True
        effective_interval = CALIBRATION_RETRY_INTERVAL if self._last_query_empty else interval
        return dt_util.utcnow() - self._last_calibrated >= effective_interval

    async def async_recalibrate(self) -> None:
        """Pull the wallbox power sensor's hourly statistics and take the
        maximum over the last WALLBOX_MAX_CHARGE_LOOKBACK_DAYS. The
        recorder query is blocking, so it runs in the executor — safe to
        call from the event loop."""
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period
        except ImportError:
            _LOGGER.debug("Wallbox max charge: recorder not available, skipping")
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=WALLBOX_MAX_CHARGE_LOOKBACK_DAYS)

        def _query() -> dict:
            return statistics_during_period(
                self._hass, start, end, {self._power_entity_id}, "hour", None, {"max"},
            )

        try:
            result = await get_instance(self._hass).async_add_executor_job(_query)
        except Exception:  # noqa: BLE001 — a calibration failure must never break switching
            _LOGGER.exception("Wallbox max charge: failed to read statistics")
            return

        points = result.get(self._power_entity_id, [])
        maxes = [p["max"] for p in points if p.get("max") is not None]
        if not maxes:
            # No history yet (fresh install) or the recorder's statistics
            # index isn't ready right at startup — retry sooner than the
            # normal cadence rather than locking in "no cap" for a full
            # day.
            _LOGGER.debug(
                "Wallbox max charge: no statistics returned for %s (queried %s to %s) "
                "— will retry sooner than the normal cadence",
                self._power_entity_id, start, end,
            )
            self._last_calibrated = dt_util.utcnow()
            self._last_query_empty = True
            return

        self._max_charge_kw = round(max(maxes), 2)
        self._sample_count = len(maxes)
        self._last_calibrated = dt_util.utcnow()
        self._last_query_empty = False
        await self._store.async_save({
            "max_charge_kw": self._max_charge_kw,
            "sample_count": self._sample_count,
            "last_calibrated": self._last_calibrated.isoformat(),
        })
        _LOGGER.info(
            "Wallbox max charge: recalibrated to %.2f kW from %d hourly point(s) "
            "over the last %d day(s)",
            self._max_charge_kw, self._sample_count, WALLBOX_MAX_CHARGE_LOOKBACK_DAYS,
        )
