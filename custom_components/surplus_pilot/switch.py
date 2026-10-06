"""Switches: car control on/off, per-device automation, departure slots."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR, CONF_CAR_NAME, CONF_DEV_ID, CONF_DEVICES, DOMAIN, NUM_SLOTS
from .entity import PilotEntity, car_info, departures_info, device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    co = hass.data[DOMAIN][entry.entry_id]
    ents = []
    car = entry.data.get(CONF_CAR)
    if car:
        cinfo = car_info(entry.entry_id, car.get(CONF_CAR_NAME) or "Auto")
        ents.append(CarControlSwitch(co, cinfo))
        ents.append(CheapSwitch(co, cinfo))
        dinfo = departures_info(entry.entry_id)
        for n in range(1, NUM_SLOTS + 1):
            ents.append(SlotSwitch(co, dinfo, n))
        ents.append(SlotSwitch(co, dinfo, 0))
    for dev in entry.data.get(CONF_DEVICES) or []:
        ents.append(DeviceAutoSwitch(co, device_info(entry.entry_id, dev), dev))
    add(ents)


class CarControlSwitch(PilotEntity, SwitchEntity):
    _attr_translation_key = "auto_steuerung"
    _attr_icon = "mdi:ev-station"

    def __init__(self, co, info):
        super().__init__(co, "auto_steuerung", info)

    @property
    def is_on(self):
        return self.coordinator.store.data["car_control"]

    async def _set(self, v: bool):
        self.coordinator.store.data["car_control"] = v
        self.coordinator.store.save()
        self.coordinator._log(f"Auto-Steuerung {'an' if v else 'aus'}")
        self.async_write_ha_state()

    async def async_turn_on(self, **kw):
        await self._set(True)

    async def async_turn_off(self, **kw):
        await self._set(False)


class DeviceAutoSwitch(PilotEntity, SwitchEntity):
    """Off = Surplus Pilot leaves this device alone (manual control)."""

    _attr_translation_key = "geraet_automatik"
    _attr_icon = "mdi:robot"

    def __init__(self, co, info, dev):
        super().__init__(co, f"{dev[CONF_DEV_ID]}_automatik", info)
        self._id = dev[CONF_DEV_ID]

    @property
    def is_on(self):
        return self.coordinator.store.data["device_enabled"].get(self._id, True)

    async def _set(self, v: bool):
        self.coordinator.store.data["device_enabled"][self._id] = v
        self.coordinator.store.save()
        self.async_write_ha_state()

    async def async_turn_on(self, **kw):
        await self._set(True)

    async def async_turn_off(self, **kw):
        await self._set(False)


def slot_ref(co, n: int) -> dict:
    return co.store.data["oneoff"] if n == 0 else co.store.data["slots"][n - 1]


def slot_key(n: int, field: str) -> str:
    return f"einmalig_{field}" if n == 0 else f"slot_{n}_{field}"


class SlotSwitch(PilotEntity, SwitchEntity):
    _attr_icon = "mdi:car-clock"

    def __init__(self, co, info, n):
        super().__init__(co, slot_key(n, "aktiv"), info)
        self._n = n
        self._attr_translation_key = "einmalig_aktiv" if n == 0 else "slot_aktiv"
        self._attr_translation_placeholders = {"n": str(n)}

    @property
    def is_on(self):
        return bool(slot_ref(self.coordinator, self._n).get("enabled"))

    async def _set(self, v: bool):
        slot_ref(self.coordinator, self._n)["enabled"] = v
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kw):
        await self._set(True)

    async def async_turn_off(self, **kw):
        await self._set(False)


class CheapSwitch(PilotEntity, SwitchEntity):
    """Grid top-up while prices are below the cheap threshold."""

    _attr_translation_key = "billig_laden"
    _attr_icon = "mdi:cash-clock"

    def __init__(self, co, info):
        super().__init__(co, "billig_laden", info)

    @property
    def is_on(self):
        return self.coordinator.store.data["cheap_enabled"]

    async def _set(self, v: bool):
        self.coordinator.store.data["cheap_enabled"] = v
        self.coordinator.store.save()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kw):
        await self._set(True)

    async def async_turn_off(self, **kw):
        await self._set(False)
