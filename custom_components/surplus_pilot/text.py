"""Departure slot names."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
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
    add([SlotName(co, info, n) for n in range(1, NUM_SLOTS + 1)])


class SlotName(PilotEntity, TextEntity):
    _attr_native_max = 40
    _attr_icon = "mdi:rename"

    def __init__(self, co, info, n):
        super().__init__(co, slot_key(n, "name"), info)
        self._n = n
        self._attr_translation_key = "slot_name"
        self._attr_translation_placeholders = {"n": str(n)}

    @property
    def native_value(self):
        return slot_ref(self.coordinator, self._n).get("name") or ""

    async def async_set_value(self, value: str) -> None:
        slot_ref(self.coordinator, self._n)["name"] = value
        self.coordinator.store.save()
        self.async_write_ha_state()
