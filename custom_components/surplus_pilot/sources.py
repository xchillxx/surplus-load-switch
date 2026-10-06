"""Reading inputs from Home Assistant: power sensors (any unit), staleness
fallback, solar forecast and price forecast. Every source here is optional
except PV and (load or grid) — nothing is tied to a vendor."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .planner import ForecastHour, PriceSlot

_LOGGER = logging.getLogger(__name__)

BAD = ("unknown", "unavailable", "", None)


def state_float(state: State | None) -> float | None:
    if state is None or state.state in BAD:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def power_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A power sensor in kW, whatever unit it reports (W, kW, MW)."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    v = state_float(st)
    if v is None:
        return None
    unit = (st.attributes.get("unit_of_measurement") or "").strip()
    if unit == "W":
        return v / 1000.0
    if unit == "MW":
        return v * 1000.0
    return v


def number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    return state_float(hass.states.get(entity_id))


def is_on(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None or st.state in BAD:
        return None
    return st.state in ("on", "home", "true", "charging", "connected")


_UNPLUGGED = {"off", "false", "disconnected", "unplugged", "not_connected", "no car connected", "none",
              "no_power", "nopower"}


def plugged(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """Plugged-in from a binary sensor (on/off) or a text sensor whose state
    names the cable/charge status (e.g. 'disconnected', 'complete',
    'charging'). None = unknown."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None or st.state in BAD:
        return None
    return st.state.strip().lower() not in _UNPLUGGED


def age_minutes(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Minutes since the sensor last reported (last_updated, not
    last_changed: a constant value that is still being reported is fresh)."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    return (dt_util.utcnow() - st.last_updated).total_seconds() / 60.0


def tracker_home(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None or st.state in BAD:
        return None
    return st.state == "home"


# ---------------------------------------------------------------- solar forecast

def _parse_wh_hours(wh_hours: dict) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for key, wh in (wh_hours or {}).items():
        try:
            ts = datetime.fromisoformat(str(key))
            if ts.tzinfo is None:
                continue
            out[ts] = out.get(ts, 0.0) + float(wh)
        except (TypeError, ValueError):
            continue
    return out


async def async_solar_forecast(hass: HomeAssistant) -> list[ForecastHour] | None:
    """Hourly PV forecast from every integration that provides one for the
    Energy dashboard (Forecast.Solar, Solcast, Open-Meteo Solar Forecast, …).
    Uses the forecast sources selected in the Energy dashboard; if none are
    selected there, every forecast-capable config entry is summed."""
    try:
        from homeassistant.components.energy.data import async_get_manager
        from homeassistant.components.energy.websocket_api import async_get_energy_platforms
    except ImportError:
        return None
    try:
        platforms = await async_get_energy_platforms(hass)
        entry_ids: list[str] = []
        manager = await async_get_manager(hass)
        for src in (manager.data or {}).get("energy_sources", []):
            if src.get("type") == "solar":
                entry_ids.extend(src.get("config_entry_solar_forecast") or [])
        if not entry_ids:
            for domain in platforms:
                entry_ids.extend(e.entry_id for e in hass.config_entries.async_entries(domain))
        total: dict[datetime, float] = {}
        for eid in dict.fromkeys(entry_ids):
            entry = hass.config_entries.async_get_entry(eid)
            if entry is None or entry.domain not in platforms:
                continue
            res = await platforms[entry.domain](hass, eid)
            for ts, wh in _parse_wh_hours((res or {}).get("wh_hours", {})).items():
                total[ts] = total.get(ts, 0.0) + wh
        if not total:
            return None
        return [ForecastHour(end=ts, kwh=wh / 1000.0) for ts, wh in sorted(total.items())]
    except Exception:  # noqa: BLE001 - the forecast improves plans, it is never required
        _LOGGER.debug("Solar forecast unavailable", exc_info=True)
        return None


# ---------------------------------------------------------------- prices

async def async_tibber_prices(hass: HomeAssistant, home: str | None, start: datetime, end: datetime) -> list[PriceSlot] | None:
    try:
        resp = await hass.services.async_call(
            "tibber", "get_prices", {"start": start.isoformat(), "end": end.isoformat()},
            blocking=True, return_response=True,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("tibber.get_prices failed", exc_info=True)
        return None
    homes = (resp or {}).get("prices") or {}
    if not homes:
        return None
    rows = homes.get(home) if home and home in homes else next(iter(homes.values()))
    pts = []
    for p in rows or []:
        try:
            pts.append((datetime.fromisoformat(p["start_time"]), float(p["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    return _to_slots(pts)


_LIST_KEYS = ("raw_today", "raw_tomorrow", "data", "prices", "forecast", "today", "tomorrow")
_START_KEYS = ("start", "start_time", "startsAt", "from", "time")
_END_KEYS = ("end", "end_time", "till", "to")
_PRICE_KEYS = ("value", "price", "price_per_kwh", "total", "price_eur_per_mwh", "price_ct_per_kwh")


def sensor_prices(hass: HomeAssistant, entity_id: str | None) -> list[PriceSlot] | None:
    """Price forecast from a sensor's attributes (Nordpool raw_today/
    raw_tomorrow, EPEX Spot data, …). Only the ORDER of prices matters for
    choosing the cheapest slots, so units don't need converting."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if st is None:
        return None
    pts: list[tuple[datetime, float, datetime | None]] = []
    for key in _LIST_KEYS:
        items = st.attributes.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            s = next((it[k] for k in _START_KEYS if k in it), None)
            e = next((it[k] for k in _END_KEYS if k in it), None)
            p = next((it[k] for k in _PRICE_KEYS if k in it), None)
            try:
                s_dt = s if isinstance(s, datetime) else dt_util.parse_datetime(str(s))
                e_dt = e if isinstance(e, datetime) or e is None else dt_util.parse_datetime(str(e))
                if s_dt is None or p is None:
                    continue
                pts.append((dt_util.as_local(s_dt), float(p), dt_util.as_local(e_dt) if e_dt else None))
            except (TypeError, ValueError):
                continue
    if not pts:
        return None
    pts.sort(key=lambda x: x[0])
    out = []
    for i, (s, p, e) in enumerate(pts):
        if e is None:
            e = pts[i + 1][0] if i + 1 < len(pts) else s + timedelta(hours=1)
        out.append(PriceSlot(s, e, p))
    return out


def _to_slots(pts: list[tuple[datetime, float]]) -> list[PriceSlot]:
    pts.sort(key=lambda x: x[0])
    out = []
    for i, (s, p) in enumerate(pts):
        e = pts[i + 1][0] if i + 1 < len(pts) else s + timedelta(minutes=15)
        if e - s > timedelta(hours=1):
            e = s + timedelta(hours=1)
        out.append(PriceSlot(s, e, p))
    return out
