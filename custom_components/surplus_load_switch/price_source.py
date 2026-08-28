"""Tibber price fetching for price-optimized minimum-runtime forcing.

Mirrors the same tibber.get_prices service-call pattern already proven
in the companion Spot Charge Scheduler add-on (same account, same
15-minute price resolution), trimmed to just what this integration
needs: given a time window and how many hours of runtime still need to
be covered before a deadline, return the start-times of the cheapest
15-minute slots that add up to that many hours.

Deliberately its own tiny module rather than folded into coordinator.py
— the Tibber service-call shape and the slot-selection math are both
self-contained and easy to reason about (and test) in isolation from
the surplus-cascade logic that consumes the result.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60.0


async def async_cheapest_slot_starts(
    hass: HomeAssistant, start: datetime, end: datetime, missing_hours: float
) -> frozenset[datetime] | None:
    """The start-times of the cheapest 15-minute slots between `start`
    and `end` whose combined duration covers `missing_hours`.

    None means the price data couldn't be fetched at all, or the
    Tibber integration isn't set up — the caller should treat that as
    "can't optimize right now" and fall back to unconditional forcing
    rather than risk missing the runtime target on missing data. An
    empty frozenset (as opposed to None) is a genuine answer: no
    hours are missing, so no slot needs to be selected.

    If the window contains fewer slots than `missing_hours` requires,
    every available slot is returned — the same "N >= available ->
    take all" degeneration the Spot Charge Scheduler's own planner
    relies on. That's not a special case to code around: it just means
    price selection no longer has any slack to work with, so the
    result is continuous forcing for the rest of the window, exactly
    the safe, deadline-guaranteeing behavior this replaces.
    """
    if missing_hours <= 0 or end <= start:
        return frozenset()

    try:
        response = await hass.services.async_call(
            "tibber", "get_prices",
            {"start": start.isoformat(), "end": end.isoformat()},
            blocking=True, return_response=True,
        )
    except Exception:  # noqa: BLE001 - a Tibber API hiccup must never crash a cycle
        _LOGGER.warning(
            "Price-optimized forcing: tibber.get_prices call failed", exc_info=True
        )
        return None

    if not response:
        return None
    prices_by_home = response.get("prices") or {}
    if not prices_by_home:
        return None
    if len(prices_by_home) > 1:
        # Multiple Tibber homes on this account — no per-device home
        # picker exists yet (not needed for a single-home account, which
        # is the only configuration this has been built/tested against).
        # Using the first one rather than guessing further is at least
        # a visible, loggable choice instead of a silent wrong answer.
        _LOGGER.warning(
            "Price-optimized forcing: multiple Tibber homes found (%s) — using the first",
            ", ".join(prices_by_home),
        )
    raw_slots = next(iter(prices_by_home.values()), [])
    if not raw_slots:
        return None

    parsed = []
    for slot in raw_slots:
        slot_start_raw = slot.get("start_time")
        slot_start = (
            slot_start_raw if isinstance(slot_start_raw, datetime)
            else dt_util.parse_datetime(slot_start_raw) if slot_start_raw else None
        )
        if slot_start is None or "price" not in slot:
            continue
        parsed.append((dt_util.as_utc(slot_start), slot["price"]))
    if not parsed:
        return None

    needed_slots = math.ceil(missing_hours / SLOT_HOURS)
    ranked = sorted(parsed, key=lambda item: item[1])
    chosen = ranked[:needed_slots]
    return frozenset(slot_start for slot_start, _price in chosen)


def slot_covers(chosen: frozenset[datetime], now: datetime) -> bool:
    """Whether `now` falls inside one of the chosen 15-minute slots."""
    return any(
        slot_start <= now < slot_start + timedelta(minutes=SLOT_MINUTES)
        for slot_start in chosen
    )
