"""Learns typical Grundlast (base_load) by weekday, hour of day, and house
mode (e.g. Abwesend/Schlafen/Urlaub) — diagnostic only for now, does not
feed any switching decision.

What gets learned, and what deliberately doesn't:
- Modes listed in LOAD_PROFILE_EXCLUDED_MODES (the daytime "Zuhause"
  mode) are neither recorded nor kept: their load is dominated by
  unpredictable spikes (wallbox, oven, ...) and carries no pattern.
- Wallbox charging is kept out entirely: samples are skipped while the
  wallbox draws power and for LOAD_PROFILE_WALLBOX_COOLDOWN_MIN afterwards
  (the wallbox reading and the house-load reading come from different
  cloud-polled integrations and don't refresh in lockstep, so the
  subtraction alone is noisy right at the ramp edges).
- The heat pump is learned separately. If a heat-pump power sensor is
  configured, its draw is subtracted from the profile above (which then
  stays a stable, weather-independent house base) and instead recorded per
  outdoor-temperature band and hour. Without that sensor the heat pump's
  draw stays inside the profile, unseparated.
- The house base uses the median per hour and across days (robust against
  one-off spikes); the heat pump uses the mean, because it cycles on and
  off within an hour and a median would report ~0 for a unit that really
  averages a few hundred watts.

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
import statistics
from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    LOAD_PROFILE_EXCLUDED_MODES,
    LOAD_PROFILE_MIN_SAMPLES,
    LOAD_PROFILE_TEMP_BANDS,
    LOAD_PROFILE_TRAILING_SAMPLES,
    LOAD_PROFILE_WALLBOX_COOLDOWN_MIN,
    LOAD_PROFILE_WALLBOX_MIN_KW,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _bucket_key(weekday: str, hour: int, mode: str) -> str:
    return f"{weekday}_{hour}_{mode}"


def _temp_band(temp_c: float) -> str:
    """Label of the LOAD_PROFILE_TEMP_BANDS interval temp_c falls into."""
    lower = None
    for edge in LOAD_PROFILE_TEMP_BANDS:
        if temp_c < edge:
            return f"<{edge:g}" if lower is None else f"{lower:g}..{edge:g}"
        lower = edge
    return f">={lower:g}"


def _wp_key(band: str, hour: int) -> str:
    return f"{band}_{hour}"


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
        # Heat-pump draw per (outdoor-temperature band, hour), same
        # rolling-window shape as _buckets. Its own in-progress
        # accumulator, since the band can change independently of the
        # weekday/mode key above.
        self._wp_buckets: dict[str, list[float]] = {}
        self._wp_current_key: tuple[str, int, date] | None = None
        self._wp_current_samples: list[float] = []
        # No sample is taken before this moment (wallbox cooldown).
        self._skip_until: datetime | None = None
        self._wp_separated = False
        self._dirty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._buckets = {
            k: list(v) for k, v in data.get("buckets", {}).items()
        }
        self._wp_buckets = {
            k: list(v) for k, v in data.get("wp_buckets", {}).items()
        }
        # Drop whatever an earlier version already learned for modes that
        # are no longer tracked.
        stale = [
            k for k in self._buckets
            if k.split("_", 2)[-1] in LOAD_PROFILE_EXCLUDED_MODES
        ]
        for k in stale:
            del self._buckets[k]
        if stale:
            self._dirty = True
            self._store.async_delay_save(self._payload, 60)

    async def async_save_now(self) -> None:
        """Force an immediate write — see coordinator.PVSurplusCoordinator.
        async_flush_stores for why this matters on unload/reload."""
        self._finalize_current(force=True)
        self._wp_finalize_current(force=True)
        if self._dirty:
            await self._store.async_save(self._payload())
            self._dirty = False

    def _payload(self) -> dict:
        return {"buckets": self._buckets, "wp_buckets": self._wp_buckets}

    def record(
        self,
        base_load_kw: float,
        mode: str | None,
        wallbox_kw: float = 0.0,
        heatpump_kw: float | None = None,
        outdoor_temp_c: float | None = None,
    ) -> None:
        """Call once per coordinator cycle. base_load_kw must already be
        free of the wallbox and every managed device (the coordinator's
        base_load_excl_wallbox). heatpump_kw / outdoor_temp_c are None
        when the corresponding optional sensor isn't configured or
        unavailable. mode is None if the house-mode entity is unavailable
        — that cycle is simply not sampled, nothing is finalized or lost."""
        if mode is None:
            return
        now = dt_util.now()

        # Wallbox: never sample while it charges or shortly afterwards.
        if wallbox_kw > LOAD_PROFILE_WALLBOX_MIN_KW:
            self._skip_until = now + timedelta(minutes=LOAD_PROFILE_WALLBOX_COOLDOWN_MIN)
        if self._skip_until is not None and now < self._skip_until:
            return

        if heatpump_kw is not None:
            self._wp_separated = True
            if outdoor_temp_c is not None:
                self._record_heatpump(now, heatpump_kw, outdoor_temp_c)
            base_load_kw = max(base_load_kw - heatpump_kw, 0.0)
        else:
            self._wp_separated = False

        if mode in LOAD_PROFILE_EXCLUDED_MODES:
            return
        key = (WEEKDAYS[now.weekday()], now.hour, mode, now.date())
        if key != self._current_key:
            self._finalize_current()
            self._current_key = key
            self._current_samples = []
        self._current_samples.append(base_load_kw)

    def _record_heatpump(self, now: datetime, heatpump_kw: float, temp_c: float) -> None:
        key = (_temp_band(temp_c), now.hour, now.date())
        if key != self._wp_current_key:
            self._wp_finalize_current()
            self._wp_current_key = key
            self._wp_current_samples = []
        self._wp_current_samples.append(heatpump_kw)

    def _wp_finalize_current(self, force: bool = False) -> None:
        if self._wp_current_key is None or not self._wp_current_samples:
            return
        band, hour, _day = self._wp_current_key
        # Mean, not median: the unit cycles on/off within the hour.
        avg = sum(self._wp_current_samples) / len(self._wp_current_samples)
        history = self._wp_buckets.setdefault(_wp_key(band, hour), [])
        history.append(round(avg, 3))
        del history[:-LOAD_PROFILE_TRAILING_SAMPLES]
        self._dirty = True
        if not force:
            self._store.async_delay_save(self._payload, 60)

    def _finalize_current(self, force: bool = False) -> None:
        if self._current_key is None or not self._current_samples:
            return
        weekday, hour, mode, _day = self._current_key
        typical = statistics.median(self._current_samples)
        bucket_key = _bucket_key(weekday, hour, mode)
        history = self._buckets.setdefault(bucket_key, [])
        history.append(round(typical, 3))
        del history[:-LOAD_PROFILE_TRAILING_SAMPLES]
        self._dirty = True
        if not force:
            self._store.async_delay_save(self._payload, 60)

    def effective_average(self, weekday: str, hour: int, mode: str) -> tuple[float | None, str]:
        """The learned average for (weekday, hour, mode), falling back to
        a coarser grouping when there isn't enough data yet. Returns
        (value, source) — source is one of "genau" (exact bucket),
        "stunde+modus" (this hour, this mode, any weekday), "stunde" (this
        hour, any weekday/mode), or "keine daten" (value is None)."""
        exact = self._buckets.get(_bucket_key(weekday, hour, mode), [])
        if len(exact) >= LOAD_PROFILE_MIN_SAMPLES:
            return statistics.median(exact), "genau"

        by_hour_mode: list[float] = []
        by_hour: list[float] = []
        for wd in WEEKDAYS:
            same_hour_mode = self._buckets.get(_bucket_key(wd, hour, mode))
            if same_hour_mode:
                by_hour_mode.extend(same_hour_mode)
        if len(by_hour_mode) >= LOAD_PROFILE_MIN_SAMPLES:
            return statistics.median(by_hour_mode), "stunde+modus"

        for key, values in self._buckets.items():
            # bucket_key format is "<weekday>_<hour>_<mode>" — the mode
            # itself may contain underscores, so only the first two parts
            # are meaningful for this comparison.
            parts = key.split("_", 2)
            if len(parts) == 3 and parts[1] == str(hour):
                by_hour.extend(values)
        if len(by_hour) >= LOAD_PROFILE_MIN_SAMPLES:
            return statistics.median(by_hour), "stunde"

        return None, "keine daten"

    @property
    def diagnostics(self) -> dict:
        total_days = sum(len(v) for v in self._buckets.values())
        return {
            "erfasste_kombinationen": len(self._buckets),
            "gesamte_tages_messwerte": total_days,
            "profil": dict(self._buckets),
            "nicht_erfasste_modi": list(LOAD_PROFILE_EXCLUDED_MODES),
            "waermepumpe_herausgerechnet": self._wp_separated,
            "waermepumpe_kw_je_temperaturband": self._wp_band_averages(),
            "waermepumpe_profil": dict(self._wp_buckets),
        }

    def _wp_band_averages(self) -> dict[str, dict[str, float]]:
        """Per temperature band: the mean heat-pump draw over all learned
        hours ("alle_stunden") and over the night hours 0-6 ("nacht") —
        the figure that matters for the overnight battery estimate."""
        per_band: dict[str, dict[str, list[float]]] = {}
        for key, values in self._wp_buckets.items():
            band, _, hour = key.rpartition("_")
            slot = per_band.setdefault(band, {"alle_stunden": [], "nacht": []})
            slot["alle_stunden"].extend(values)
            if hour.isdigit() and int(hour) <= 6:
                slot["nacht"].extend(values)
        return {
            band: {
                name: round(sum(v) / len(v), 3)
                for name, v in slots.items() if v
            }
            for band, slots in per_band.items()
        }
