"""Departure times."""
from __future__ import annotations

from datetime import time as dtime

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR, DOMAIN, NUM_SLOTS
from .departures import parse_hhmm
from .entity import PilotEntity, departures_info
from .switch import slot_key, slot_ref


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    if not entry.data.get(CONF_CAR):
        return
    co = hass.data[DOMAIN][entry.entry_id]
    info = departures_info(entry.entry_id)
    add([SlotTime(co, info, n) for n in range(0, NUM_SLOTS + 1)])


class SlotTime(PilotEntity, TimeEntity):
    _attr_icon = "mdi:clock-outline"

    def __init__(self, co, info, n):
        super().__init__(co, slot_key(n, "uhrzeit"), info)
        self._n = n
        self._attr_translation_key = "einmalig_uhrzeit" if n == 0 else "slot_uhrzeit"
        self._attr_translation_placeholders = {"n": str(n)}

    @property
    def native_value(self):
        h, m = parse_hhmm(slot_ref(self.coordinator, self._n).get("time"))
        return dtime(h, m)

    async def async_set_value(self, value: dtime) -> None:
        slot_ref(self.coordinator, self._n)["time"] = value.strftime("%H:%M")
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
