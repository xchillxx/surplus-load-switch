"""Persistent runtime state (what the dashboard changes and what is learned).

Kept out of the config entry on purpose: changing a departure or switching
a device's automation must not reload the integration (that would interrupt
a running charge and reset every debounce timer)."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_NIGHT_BASE_KW,
    DEFAULT_SLOT_TARGET,
    DEFAULT_SLOT_TIME,
    DOMAIN,
    MODE_AUTO,
    NUM_SLOTS,
    STORAGE_VERSION,
)

SAVE_DELAY = 30


def _default_slot(n: int) -> dict:
    return {"slot": n, "enabled": False, "name": "", "target_soc": DEFAULT_SLOT_TARGET,
            "time": DEFAULT_SLOT_TIME, "away_h": 0.0, "rhythm_days": 1, "start_date": None}


class PilotStore:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}")
        self.data: dict = {}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        data.setdefault("mode", MODE_AUTO)
        data.setdefault("car_control", True)
        data.setdefault("device_enabled", {})
        slots = data.get("slots") or []
        by_no = {s.get("slot"): s for s in slots if isinstance(s, dict)}
        data["slots"] = [{**_default_slot(n), **by_no.get(n, {})} for n in range(1, NUM_SLOTS + 1)]
        data.setdefault("oneoff", {"enabled": False, "name": "Einmalig", "target_soc": DEFAULT_SLOT_TARGET,
                                   "time": DEFAULT_SLOT_TIME, "away_h": 0.0, "start_date": None})
        data.setdefault("night_base_kw", DEFAULT_NIGHT_BASE_KW)
        data.setdefault("runtime", {"date": dt_util.now().date().isoformat(), "seconds": {}})
        data.setdefault("learned_kw", {})
        data.setdefault("price_archive", {})
        data.setdefault("cheap_enabled", True)
        data.setdefault("cheap_percentile", 10)
        data.setdefault("cheap_target", 80)
        data.setdefault("commands", {"date": dt_util.now().date().isoformat(), "count": 0})
        self.data = data

    def save(self) -> None:
        self._store.async_delay_save(lambda: self.data, SAVE_DELAY)

    async def async_save_now(self) -> None:
        await self._store.async_save(self.data)

    # ---- day roll-over
    def roll_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if self.data["runtime"]["date"] != today:
            self.data["runtime"] = {"date": today, "seconds": {}}
        if self.data["commands"]["date"] != today:
            self.data["commands"] = {"date": today, "count": 0}

    def add_runtime(self, dev_id: str, seconds: float) -> None:
        sec = self.data["runtime"]["seconds"]
        sec[dev_id] = sec.get(dev_id, 0.0) + seconds

    def runtime_h(self, dev_id: str) -> float:
        return self.data["runtime"]["seconds"].get(dev_id, 0.0) / 3600.0

    def count_command(self) -> None:
        self.roll_day()
        self.data["commands"]["count"] += 1
        self.save()

    @property
    def commands_today(self) -> int:
        self.roll_day()
        return self.data["commands"]["count"]

    def learn_power(self, dev_id: str, kw: float, weight: float = 0.02) -> None:
        """Slow average of a device's measured draw while it runs."""
        old = self.data["learned_kw"].get(dev_id)
        self.data["learned_kw"][dev_id] = kw if old is None else old + weight * (kw - old)

    def learn_night_base(self, kw: float, weight: float = 0.01) -> None:
        old = self.data["night_base_kw"]
        self.data["night_base_kw"] = old + weight * (kw - old)

    # ---- price archive (published prices are kept; Tibber can't return past ones)
    def archive_prices(self, slots, now, days: int) -> None:
        arch = self.data["price_archive"]
        for p in slots:
            if p.start < now:
                arch[p.start.isoformat()] = p.price
        cutoff = (now - timedelta(days=days)).isoformat()
        for k in [k for k in arch if k < cutoff]:
            arch.pop(k)
