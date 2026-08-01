"""Self-calibrating solar-start offsets, learned per calendar month from
this system's own historical solar production.

Home Assistant's long-term statistics already retain hourly mean/min/max for
the solar sensor indefinitely (independent of the recorder's raw-history
purge period), so this needs no sample storage of its own — it periodically
re-derives each month's offset straight from the recorder's statistics.

Cloud filtering is self-referential and needs no external weather data: a
day only counts if its peak production reaches at least 80% of the 90th
percentile peak in a surrounding +/-10 day window. That adapts to the
system's own seasonal capacity swing instead of a fixed threshold, so a
string of overcast days doesn't get treated as "this is just how mornings
are here".

Within a "good" day, "solar start" is the first hour whose mean production
reaches 15% of that day's own peak — a percentage of the day's own peak,
not a fixed kW value, so the same logic works unmodified on a 3 kWp system
and a 15 kWp one.

Any month without enough good-quality days yet (a brand new install won't
have last November's data) simply keeps using the configured/default
estimate for that month, so the whole thing improves progressively over a
year of real operation instead of requiring a full year before it does
anything at all.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CALIBRATION_CLOUD_GOOD_RATIO,
    CALIBRATION_CLOUD_WINDOW_DAYS,
    CALIBRATION_LOOKBACK_DAYS,
    CALIBRATION_MAX_INTERP_MONTHS,
    CALIBRATION_MIN_GOOD_DAYS,
    CALIBRATION_RETRY_INTERVAL,
    CALIBRATION_THRESHOLD_RATIO,
    RECENT_SOC_GAIN_WINDOW_DAYS,
    SOLAR_START_MIN_KW,
    STORAGE_VERSION,
    WEAK_DAY_EARLIEST_CHECK_HOUR,
)

_LOGGER = logging.getLogger(__name__)


class SolarOffsetCalibrator:
    """Learns, per calendar month (1-12), hours after astronomical sunrise
    until this system's solar production is meaningful — falling back to a
    caller-supplied default for any month without enough good history yet.
    """

    def __init__(
        self, hass: HomeAssistant, entry_id: str, solar_entity_id: str, soc_entity_id: str
    ) -> None:
        self._hass = hass
        self._solar_entity_id = solar_entity_id
        self._soc_entity_id = soc_entity_id
        self._store: Store = Store(hass, STORAGE_VERSION, f"surplus_load_switch_calibration_{entry_id}")
        self._offsets: dict[int, float] = {}
        self._good_day_counts: dict[int, int] = {}
        # Median good-day battery-SOC gain (percentage points) from solar
        # start to WEAK_DAY_EARLIEST_CHECK_HOUR, per calendar month — a
        # "how much does the battery normally pick up by this hour, this
        # time of year" reference, used by the coordinator's weak-day
        # detection — otherwise unrelated to the solar-start offset this
        # class exists for, but computed from the exact same good-day data
        # so it made no sense to duplicate the query/filtering.
        self._reference_soc_gains: dict[int, float] = {}
        # Median SOC gain over the last RECENT_SOC_GAIN_WINDOW_DAYS
        # calendar days, unfiltered (no cloud/good-day logic) — a much
        # simpler, always-available fallback reference for weak-day
        # detection whenever the current month doesn't have its own
        # calibrated reference yet (e.g. right after install, or a system
        # too young for a full month of good-day data). Computed from the
        # exact same query as the calibration above, just without its
        # filtering.
        self._recent_soc_gain: float | None = None
        self._last_calibrated: datetime | None = None
        self._last_sources: dict[int, str] = {}
        self._last_query_empty = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._offsets = {int(k): v for k, v in data.get("offsets", {}).items()}
        self._good_day_counts = {int(k): v for k, v in data.get("good_day_counts", {}).items()}
        self._reference_soc_gains = {
            int(k): v for k, v in data.get("reference_soc_gains", {}).items()
        }
        self._recent_soc_gain = data.get("recent_soc_gain")
        last = data.get("last_calibrated")
        self._last_calibrated = dt_util.parse_datetime(last) if last else None
        # Storage written by a pre-SOC-gain version of this integration has
        # no "reference_soc_gains" key at all (not just an empty one) —
        # treat that the same as never having calibrated, so a version
        # upgrade recalibrates on its very next cycle instead of silently
        # running on stale/absent weak-day data for up to the normal 24h
        # interval. A fresh install's empty Store never reaches this line
        # (caught by "if not data: return" above), so this can't misfire
        # there.
        if "reference_soc_gains" not in data:
            self._last_calibrated = None

    def reference_soc_gain(self, month: int) -> float | None:
        """This month's typical good-day SOC gain from solar start to
        WEAK_DAY_EARLIEST_CHECK_HOUR, or None if it hasn't been
        calibrated yet."""
        return self._reference_soc_gains.get(month)

    def effective_reference_soc_gain(self, month: int) -> float | None:
        """The calibrated monthly reference if there is one, otherwise the
        simple last-N-days fallback — whichever is available. What weak-
        day detection should actually call."""
        return self._reference_soc_gains.get(month) or self._recent_soc_gain

    def offsets_for(self, configured_defaults: list[float]) -> list[float]:
        """12-element list (Jan..Dec).

        A directly calibrated month uses its own learned value. A month
        without enough data yet borrows from its nearest calibrated
        neighbour(s) — linearly interpolated if there's a calibrated month
        within CALIBRATION_MAX_INTERP_MONTHS on *both* sides (circularly,
        December wraps to January), or just the single nearest one if only
        one side is close enough. Solar offset changes gradually across the
        year (it tracks the sun's elevation angle, not a step function), so
        this is a meaningfully better guess than the static default for a
        month that's only one or two months away from real data — but we
        don't trust it across a large gap (e.g. bridging from July across
        an entirely unobserved autumn/winter to reach January) since the
        seasonal relationship isn't necessarily linear over half a year.
        Falls back to the configured/default estimate wherever no
        calibrated month is within range yet.
        """
        sources: dict[int, str] = {}
        result: list[float] = []
        calibrated_months = sorted(self._offsets.keys())
        for m in range(1, 13):
            if m in self._offsets:
                sources[m] = "gemessen"
                result.append(self._offsets[m])
                continue
            interpolated = self._interpolate(m, calibrated_months)
            if interpolated is not None:
                value, source = interpolated
                sources[m] = source
                result.append(value)
            else:
                sources[m] = "standard"
                result.append(configured_defaults[m - 1])
        self._last_sources = sources
        return result

    def _interpolate(self, month: int, calibrated_months: list[int]) -> tuple[float, str] | None:
        """Find the nearest calibrated month going forward (increasing,
        wrapping Dec->Jan) and backward (decreasing, wrapping Jan->Dec)
        from `month`, each only if within CALIBRATION_MAX_INTERP_MONTHS
        steps. Interpolates between both if both exist; otherwise carries
        forward the single closer one; otherwise gives up."""
        if not calibrated_months:
            return None

        before, dist_before = None, None
        after, dist_after = None, None
        for cm in calibrated_months:
            forward = (cm - month) % 12  # steps from `month` to `cm` going forward
            backward = (month - cm) % 12  # steps from `month` to `cm` going backward
            if forward <= CALIBRATION_MAX_INTERP_MONTHS and (dist_after is None or forward < dist_after):
                after, dist_after = cm, forward
            if backward <= CALIBRATION_MAX_INTERP_MONTHS and (dist_before is None or backward < dist_before):
                before, dist_before = cm, backward

        if before is not None and after is not None and before != after:
            total = dist_before + dist_after
            weight_after = dist_before / total
            value = self._offsets[before] * (1 - weight_after) + self._offsets[after] * weight_after
            return value, f"interpoliert ({before}->{after})"

        nearest = before if dist_before is not None and (dist_after is None or dist_before <= dist_after) else after
        if nearest is not None:
            return self._offsets[nearest], f"übernommen (Monat {nearest})"

        return None

    @property
    def diagnostics(self) -> dict:
        return {
            "kalibrierte_monate": sorted(self._offsets.keys()),
            "gelernte_werte_h": dict(self._offsets),
            "gute_tage_pro_monat": dict(self._good_day_counts),
            "quelle_pro_monat": dict(self._last_sources),
            "referenz_soc_zuwachs_pro_monat": dict(self._reference_soc_gains),
            "referenz_soc_zuwachs_letzte_14_tage": self._recent_soc_gain,
            "zuletzt_kalibriert": self._last_calibrated.isoformat() if self._last_calibrated else None,
        }

    def due_for_recalibration(self, interval: timedelta) -> bool:
        if self._last_calibrated is None:
            return True
        effective_interval = CALIBRATION_RETRY_INTERVAL if self._last_query_empty else interval
        return dt_util.utcnow() - self._last_calibrated >= effective_interval

    async def async_recalibrate(self) -> None:
        """Pull long-term statistics and recompute per-month offsets.

        The recorder query and the astral/grouping math are both blocking,
        so both run in the executor — safe to call from the event loop.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period
        except ImportError:
            _LOGGER.debug("Solar offset calibration: recorder not available, skipping")
            return

        end = dt_util.utcnow()
        start = end - timedelta(days=CALIBRATION_LOOKBACK_DAYS)

        def _query() -> dict:
            return statistics_during_period(
                self._hass, start, end,
                {self._solar_entity_id, self._soc_entity_id}, "hour", None, {"mean"},
            )

        try:
            result = await get_instance(self._hass).async_add_executor_job(_query)
        except Exception:  # noqa: BLE001 — a calibration failure must never break switching
            _LOGGER.exception("Solar offset calibration: failed to read statistics")
            return

        solar_points = result.get(self._solar_entity_id, [])
        soc_points = result.get(self._soc_entity_id, [])
        if not solar_points:
            # Doesn't set _last_calibrated to a value that blocks the normal
            # 24h cadence — an empty result right after startup (recorder
            # or its statistics index not fully ready yet) should be
            # retried soon, not locked out for a full day. Once a real
            # result comes back (even with 0 calibrated months from too
            # little good data), the normal cadence takes over.
            _LOGGER.warning(
                "Solar offset calibration: no statistics returned for %s "
                "(queried %s to %s, result had keys: %s) — will retry sooner than the normal 24h cadence",
                self._solar_entity_id, start, end, list(result.keys()),
            )
            self._last_calibrated = dt_util.utcnow()
            self._last_query_empty = True
            return

        try:
            offsets, good_counts, reference_soc_gains, recent_soc_gain = (
                await self._hass.async_add_executor_job(self._compute, solar_points, soc_points)
            )
        except Exception:  # noqa: BLE001 — same: never let this break the coordinator
            _LOGGER.exception("Solar offset calibration: failed to compute offsets")
            return

        self._offsets = offsets
        self._good_day_counts = good_counts
        self._reference_soc_gains = reference_soc_gains
        self._recent_soc_gain = recent_soc_gain
        self._last_calibrated = dt_util.utcnow()
        self._last_query_empty = False
        await self._store.async_save({
            "offsets": self._offsets,
            "good_day_counts": self._good_day_counts,
            "reference_soc_gains": self._reference_soc_gains,
            "recent_soc_gain": self._recent_soc_gain,
            "last_calibrated": self._last_calibrated.isoformat(),
        })
        _LOGGER.info(
            "Solar offset calibration: %d month(s) calibrated from %d good day(s) total, "
            "recent-14-day reference SOC gain %s",
            len(self._offsets), sum(good_counts.values()),
            f"{recent_soc_gain:.1f} %" if recent_soc_gain is not None else "not enough data yet",
        )

    def _compute(
        self, solar_points: list[dict], soc_points: list[dict]
    ) -> tuple[dict[int, float], dict[int, int], dict[int, float], float | None]:
        """CPU-bound: sunrise lookup + day grouping + the cloud filter.
        Must run in the executor, not the event loop."""
        from astral import LocationInfo
        from astral.sun import sun

        tz = dt_util.get_time_zone(self._hass.config.time_zone) or dt_util.UTC
        loc = LocationInfo(
            latitude=self._hass.config.latitude,
            longitude=self._hass.config.longitude,
        )

        by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
        for p in solar_points:
            mean = p.get("mean")
            if mean is None:
                continue
            start_dt = dt_util.utc_from_timestamp(p["start"] / 1000).astimezone(tz)
            by_day[start_dt.date()].append((start_dt, mean))

        by_day_soc: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
        for p in soc_points:
            mean = p.get("mean")
            if mean is None:
                continue
            start_dt = dt_util.utc_from_timestamp(p["start"] / 1000).astimezone(tz)
            by_day_soc[start_dt.date()].append((start_dt, mean))

        days = sorted(by_day.keys())
        peaks = {d: max(v for _, v in by_day[d]) for d in days}

        # Per-day SOC gain from solar start (first hour reaching
        # SOLAR_START_MIN_KW — the same live-computable threshold the
        # coordinator uses, not a fraction of that day's peak, since this
        # has to match what the live tracking can actually know in the
        # moment) to WEAK_DAY_EARLIEST_CHECK_HOUR local time. Computed for
        # every day with both solar and SOC data, independent of the
        # cloud/good-day filter below — that filter is applied separately
        # per use (calibrated monthly reference vs. simple recent-window
        # fallback).
        day_soc_gain: dict[date, float] = {}
        for d in days:
            solar_entries = sorted(by_day[d])
            soc_entries = sorted(by_day_soc.get(d, []))
            if not soc_entries:
                continue
            start_t = next((t for t, v in solar_entries if v >= SOLAR_START_MIN_KW), None)
            if start_t is None:
                continue
            ref_t = start_t.replace(
                hour=WEAK_DAY_EARLIEST_CHECK_HOUR, minute=0, second=0, microsecond=0
            )
            if ref_t <= start_t:
                continue  # solar started at/after the reference hour that day
            soc_start = next((v for t, v in soc_entries if t >= start_t), None)
            soc_at_ref = next((v for t, v in soc_entries if t >= ref_t), None)
            if soc_start is None or soc_at_ref is None:
                continue
            day_soc_gain[d] = soc_at_ref - soc_start

        # Simple recent-days fallback reference — no cloud/good-day
        # filtering, just the last RECENT_SOC_GAIN_WINDOW_DAYS *complete*
        # calendar days (today's excluded: it isn't over yet).
        today = dt_util.now(tz).date()
        recent_days = [d for d in days if d < today][-RECENT_SOC_GAIN_WINDOW_DAYS:]
        recent_gains = [day_soc_gain[d] for d in recent_days if d in day_soc_gain]
        recent_soc_gain = (
            round(statistics.median(recent_gains), 3) if len(recent_gains) >= 5 else None
        )

        good_days: list[date] = []
        rejected_no_neighbors = 0
        rejected_too_cloudy = 0
        for i, d in enumerate(days):
            window = days[max(0, i - CALIBRATION_CLOUD_WINDOW_DAYS): i + CALIBRATION_CLOUD_WINDOW_DAYS + 1]
            window_peaks = sorted(peaks[wd] for wd in window if wd != d)
            if len(window_peaks) < 5 or peaks[d] <= 0:
                rejected_no_neighbors += 1
                continue
            reference = window_peaks[int(len(window_peaks) * 0.9)]
            if reference > 0 and peaks[d] >= CALIBRATION_CLOUD_GOOD_RATIO * reference:
                good_days.append(d)
            else:
                rejected_too_cloudy += 1

        by_month: dict[int, list[float]] = defaultdict(list)
        by_month_soc_gain: dict[int, list[float]] = defaultdict(list)
        rejected_no_threshold_hour = 0
        rejected_no_sunrise = 0
        for d in good_days:
            if d in day_soc_gain:
                by_month_soc_gain[d.month].append(day_soc_gain[d])
            entries = sorted(by_day[d])
            threshold = CALIBRATION_THRESHOLD_RATIO * peaks[d]
            first_above = next((t for t, v in entries if v >= threshold), None)
            if first_above is None:
                rejected_no_threshold_hour += 1
                continue
            try:
                sunrise = sun(loc.observer, date=d, tzinfo=tz)["sunrise"]
            except Exception:  # noqa: BLE001 — polar day/night etc., just skip that day
                rejected_no_sunrise += 1
                continue
            offset_h = (first_above - sunrise).total_seconds() / 3600.0
            by_month[d.month].append(offset_h)

        offsets: dict[int, float] = {}
        good_counts: dict[int, int] = {}
        for m, vals in by_month.items():
            good_counts[m] = len(vals)
            if len(vals) >= CALIBRATION_MIN_GOOD_DAYS:
                offsets[m] = round(statistics.median(vals), 2)

        reference_soc_gains: dict[int, float] = {
            m: round(statistics.median(vals), 3)
            for m, vals in by_month_soc_gain.items()
            if len(vals) >= CALIBRATION_MIN_GOOD_DAYS
        }

        _LOGGER.info(
            "Solar offset calibration detail: %d day(s) with data, %d good day(s) "
            "(%d rejected: too few neighbouring days with data, %d rejected: too "
            "cloudy vs. neighbours), %d rejected (no hour reached the start "
            "threshold), %d rejected (sunrise lookup failed) -- %d month(s) got a "
            "calibrated offset, %d month(s) got a reference SOC gain, %d recent "
            "day(s) used for the fallback reference",
            len(days), len(good_days), rejected_no_neighbors, rejected_too_cloudy,
            rejected_no_threshold_hour, rejected_no_sunrise, len(offsets), len(reference_soc_gains),
            len(recent_gains),
        )

        return offsets, good_counts, reference_soc_gains, recent_soc_gain
