"""Departure start dates (rhythm phase / one-off date)."""
from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR, DOMAIN, NUM_SLOTS
from .entity import PilotEntity, departures_info
from .switch import slot_key, slot_ref


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    if not entry.data.get(CONF_CAR):
        return
    co = hass.data[DOMAIN][entry.entry_id]
    info = departures_info(entry.entry_id)
    add([SlotDate(co, info, n) for n in range(0, NUM_SLOTS + 1)])


class SlotDate(PilotEntity, DateEntity):
    _attr_icon = "mdi:calendar-start"

    def __init__(self, co, info, n):
        super().__init__(co, slot_key(n, "datum"), info)
        self._n = n
        self._attr_translation_key = "einmalig_datum" if n == 0 else "slot_datum"
        self._attr_translation_placeholders = {"n": str(n)}

    @property
    def native_value(self):
        v = slot_ref(self.coordinator, self._n).get("start_date")
        try:
            return date.fromisoformat(v) if v else None
        except ValueError:
            return None

    async def async_set_value(self, value: date) -> None:
        slot_ref(self.coordinator, self._n)["start_date"] = value.isoformat()
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
