"""Departure slots -> concrete departures (pure logic, no HA imports).

A slot is a dict:
    {slot, enabled, name, target_soc, time "HH:MM", away_h, rhythm_days, start_date "YYYY-MM-DD"}
rhythm_days == 0: one-off on start_date (or the next time-of-day if no date);
rhythm_days >= 1: every N days counted from start_date.
away_h: how long the car is usually gone (0 = unknown). Only used to know
whether the car can still catch PV tomorrow.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .planner import Departure

LOOKAHEAD_DAYS = 4


def parse_hhmm(value: str | None, default: tuple[int, int] = (7, 0)) -> tuple[int, int]:
    try:
        hh, mm = str(value).split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def expand_slot(slot: dict, now: datetime, grace_h: float = 1.0) -> list[Departure]:
    if not slot.get("enabled"):
        return []
    h, m = parse_hhmm(slot.get("time"))
    try:
        target = float(slot.get("target_soc"))
    except (TypeError, ValueError):
        target = 50.0
    rhythm = int(slot.get("rhythm_days") or 0)
    away = float(slot.get("away_h") or 0.0)
    name = (slot.get("name") or "").strip() or f"Slot {slot.get('slot', '?')}"
    tz = now.tzinfo
    start_day = _parse_date(slot.get("start_date")) or now.date()

    def at(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)

    out: list[datetime] = []
    earliest = now - timedelta(hours=grace_h)
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    if rhythm <= 0:
        t = at(start_day)
        if slot.get("start_date") is None and t < earliest:
            t += timedelta(days=1)
        if earliest <= t <= horizon:
            out.append(t)
    else:
        d = start_day
        if d < now.date():
            steps = (now.date() - d).days // rhythm
            d = d + timedelta(days=steps * rhythm)
        while at(d) < earliest:
            d += timedelta(days=rhythm)
        while at(d) <= horizon:
            out.append(at(d))
            d += timedelta(days=rhythm)
    return [Departure(start=t, target_soc=target, name=name,
                      returns=(t + timedelta(hours=away)) if away > 0 else None) for t in out]


def all_departures(slots: list[dict], oneoff: dict | None, now: datetime) -> list[Departure]:
    deps: list[Departure] = []
    for s in slots:
        deps.extend(expand_slot(s, now))
    if oneoff and oneoff.get("enabled") and oneoff.get("start_date"):
        deps.extend(expand_slot({**oneoff, "rhythm_days": 0, "name": oneoff.get("name") or "Einmalig"}, now))
    deps.sort(key=lambda d: d.start)
    return deps
