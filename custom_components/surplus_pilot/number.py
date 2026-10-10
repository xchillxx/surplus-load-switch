"""Numbers: departure targets, away duration, rhythm."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR, DOMAIN, NUM_SLOTS
from .const import CONF_CAR_NAME
from .entity import PilotEntity, car_info, departures_info
from .switch import slot_key, slot_ref

FIELDS = {
    "target_soc": ("ziel_soc", 5, 100, 5, "%", "mdi:battery-charging-60"),
    "away_h": ("weg_h", 0, 48, 0.5, "h", "mdi:timer-sand"),
    "rhythm_days": ("rhythmus", 0, 28, 1, "d", "mdi:calendar-sync"),
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    if not entry.data.get(CONF_CAR):
        return
    co = hass.data[DOMAIN][entry.entry_id]
    info = departures_info(entry.entry_id)
    ents = []
    for n in range(0, NUM_SLOTS + 1):
        for field in FIELDS:
            if n == 0 and field == "rhythm_days":
                continue
            ents.append(SlotNumber(co, info, n, field))
    cinfo = car_info(entry.entry_id, entry.data[CONF_CAR].get(CONF_CAR_NAME) or "Auto")
    ents.append(StoreNumber(co, cinfo, "billig_perzentil", "cheap_percentile", 1, 50, 1, "%", "mdi:percent"))
    ents.append(StoreNumber(co, cinfo, "billig_bis", "cheap_target", 20, 100, 5, "%", "mdi:battery-arrow-up"))
    ents.append(StoreNumber(co, cinfo, "billig_pv_ab_akku", "cheap_pv_battery_soc", 0, 100, 5, "%",
                            "mdi:home-battery"))
    add(ents)


class SlotNumber(PilotEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, co, info, n, field):
        key, lo, hi, step, unit, icon = FIELDS[field]
        super().__init__(co, slot_key(n, key), info)
        self._n, self._field = n, field
        self._attr_translation_key = ("einmalig_" if n == 0 else "slot_") + key
        self._attr_translation_placeholders = {"n": str(n)}
        self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step = lo, hi, step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        return slot_ref(self.coordinator, self._n).get(self._field)

    async def async_set_native_value(self, value: float) -> None:
        slot_ref(self.coordinator, self._n)[self._field] = int(value) if self._field == "rhythm_days" else value
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class StoreNumber(PilotEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, co, info, key, field, lo, hi, step, unit, icon):
        super().__init__(co, key, info)
        self._field = field
        self._attr_translation_key = key
        self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step = lo, hi, step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        return self.coordinator.store.data[self._field]

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.store.data[self._field] = value
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
