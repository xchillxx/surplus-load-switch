"""Config flow: energy sensors first, then car and devices via the options menu.
Every role is a free entity choice — nothing is tied to a vendor."""
from __future__ import annotations

import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    CLIMATE_MODES,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAR,
    CONF_CAR_ALLOW_GRID,
    CONF_CAR_CAPACITY_KWH,
    CONF_CAR_CHARGE_SWITCH,
    CONF_CAR_CHARGING_SENSOR,
    CONF_CAR_CURRENT_ENTITY,
    CONF_CAR_EFFICIENCY,
    CONF_CAR_LIMIT_DEFAULT,
    CONF_CAR_LIMIT_ENTITY,
    CONF_CAR_MAX_A,
    CONF_CAR_MAX_COMMANDS,
    CONF_CAR_MIN_A,
    CONF_CAR_NAME,
    CONF_CAR_PAUSE_ENTITY,
    CONF_CAR_PAUSE_STATE,
    CONF_CAR_PHASES,
    CONF_CAR_PLUGGED_FALLBACK,
    CONF_CAR_PLUGGED_SENSOR,
    CONF_CAR_POWER_SENSOR,
    CONF_CAR_SOC_FALLBACK,
    CONF_CAR_SOC_SENSOR,
    CONF_CAR_TRACKER,
    CONF_CAR_TRACKER_FALLBACK,
    CONF_CAR_VOLTAGE,
    CONF_DEV_CLIMATE_MODE,
    CONF_DEV_DEPENDS_ON,
    CONF_DEV_ENTITY,
    CONF_DEV_ID,
    CONF_DEV_KIND,
    CONF_DEV_MIN_RUNTIME_H,
    CONF_DEV_NAME,
    CONF_DEV_POWER_KW,
    CONF_DEV_POWER_SENSOR,
    CONF_DEV_PRIORITY,
    CONF_DEV_SCHEDULE,
    CONF_DEV_SOC_RESERVE,
    CONF_DEV_WINDOW_END,
    CONF_DEV_WINDOW_START,
    CONF_DEVICES,
    CONF_GRID_EXPORT_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_PRICE_HISTORY_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PRICE_SOURCE,
    CONF_PV_END_BEFORE_SUNSET_H,
    CONF_PV_SENSOR,
    CONF_STALE_MINUTES,
    CONF_TIBBER_HOME,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_CAR_CAPACITY_KWH,
    DEFAULT_CAR_EFFICIENCY,
    DEFAULT_CAR_LIMIT,
    DEFAULT_CAR_MAX_A,
    DEFAULT_CAR_MIN_A,
    DEFAULT_CAR_PHASES,
    DEFAULT_CAR_VOLTAGE,
    DEFAULT_PV_END_BEFORE_SUNSET_H,
    DEFAULT_STALE_MINUTES,
    DOMAIN,
    KIND_CLIMATE,
    KIND_SWITCH,
    PRICE_NONE,
    PRICE_SENSOR,
    PRICE_TIBBER,
)


def _d(d: dict, key: str) -> dict:
    """default= only for real values (an empty optional field must stay empty)."""
    v = d.get(key)
    return {"default": v} if v not in (None, "") else {}


def _ent(domain, device_class=None):
    cfg = sel.EntitySelectorConfig(domain=domain)
    if device_class:
        cfg = sel.EntitySelectorConfig(domain=domain, device_class=device_class)
    return sel.EntitySelector(cfg)


def _num(lo, hi, step, unit=None):
    kw = {"unit_of_measurement": unit} if unit else {}
    return sel.NumberSelector(sel.NumberSelectorConfig(min=lo, max=hi, step=step, mode=sel.NumberSelectorMode.BOX,
                                                       **kw))


def energy_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_PV_SENSOR, **_d(d, CONF_PV_SENSOR)): _ent("sensor"),
        vol.Required(CONF_LOAD_SENSOR, **_d(d, CONF_LOAD_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_GRID_EXPORT_SENSOR, **_d(d, CONF_GRID_EXPORT_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_BATTERY_SOC_SENSOR, **_d(d, CONF_BATTERY_SOC_SENSOR)): _ent("sensor"),
        vol.Required(CONF_BATTERY_CAPACITY_KWH, default=d.get(CONF_BATTERY_CAPACITY_KWH,
                                                              DEFAULT_BATTERY_CAPACITY_KWH)): _num(0, 200, 0.1, "kWh"),
        vol.Required(CONF_BATTERY_MIN_SOC, default=d.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)):
            _num(0, 80, 1, "%"),
        vol.Required(CONF_PV_END_BEFORE_SUNSET_H, default=d.get(CONF_PV_END_BEFORE_SUNSET_H,
                                                                DEFAULT_PV_END_BEFORE_SUNSET_H)): _num(0, 4, 0.25, "h"),
        vol.Required(CONF_STALE_MINUTES, default=d.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES)):
            _num(5, 120, 1, "min"),
        vol.Required(CONF_PRICE_SOURCE, default=d.get(CONF_PRICE_SOURCE, PRICE_NONE)): sel.SelectSelector(
            sel.SelectSelectorConfig(options=[PRICE_NONE, PRICE_TIBBER, PRICE_SENSOR], translation_key="price_source",
                                     mode=sel.SelectSelectorMode.DROPDOWN)),
        vol.Optional(CONF_TIBBER_HOME, **_d(d, CONF_TIBBER_HOME)): str,
        vol.Optional(CONF_PRICE_SENSOR, **_d(d, CONF_PRICE_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_PRICE_HISTORY_SENSOR, **_d(d, CONF_PRICE_HISTORY_SENSOR)): _ent("sensor"),
    })


def car_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_CAR_NAME, default=d.get(CONF_CAR_NAME, "Auto")): str,
        vol.Required(CONF_CAR_CHARGE_SWITCH, **_d(d, CONF_CAR_CHARGE_SWITCH)): _ent("switch"),
        vol.Optional(CONF_CAR_CURRENT_ENTITY, **_d(d, CONF_CAR_CURRENT_ENTITY)): _ent("number"),
        vol.Optional(CONF_CAR_SOC_SENSOR, **_d(d, CONF_CAR_SOC_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_CAR_SOC_FALLBACK, **_d(d, CONF_CAR_SOC_FALLBACK)): _ent("sensor"),
        vol.Optional(CONF_CAR_LIMIT_ENTITY, **_d(d, CONF_CAR_LIMIT_ENTITY)): _ent(["number", "sensor"]),
        vol.Required(CONF_CAR_LIMIT_DEFAULT, default=d.get(CONF_CAR_LIMIT_DEFAULT, DEFAULT_CAR_LIMIT)):
            _num(10, 100, 1, "%"),
        vol.Optional(CONF_CAR_PLUGGED_SENSOR, **_d(d, CONF_CAR_PLUGGED_SENSOR)): _ent(["binary_sensor", "sensor"]),
        vol.Optional(CONF_CAR_PLUGGED_FALLBACK, **_d(d, CONF_CAR_PLUGGED_FALLBACK)): _ent(["binary_sensor", "sensor"]),
        vol.Optional(CONF_CAR_CHARGING_SENSOR, **_d(d, CONF_CAR_CHARGING_SENSOR)): _ent(["binary_sensor", "sensor"]),
        vol.Optional(CONF_CAR_POWER_SENSOR, **_d(d, CONF_CAR_POWER_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_CAR_TRACKER, **_d(d, CONF_CAR_TRACKER)): _ent(["device_tracker", "person"]),
        vol.Optional(CONF_CAR_TRACKER_FALLBACK, **_d(d, CONF_CAR_TRACKER_FALLBACK)): _ent(["device_tracker", "person"]),
        vol.Required(CONF_CAR_CAPACITY_KWH, default=d.get(CONF_CAR_CAPACITY_KWH, DEFAULT_CAR_CAPACITY_KWH)):
            _num(5, 250, 0.1, "kWh"),
        vol.Required(CONF_CAR_EFFICIENCY, default=d.get(CONF_CAR_EFFICIENCY, DEFAULT_CAR_EFFICIENCY)):
            _num(0.5, 1.0, 0.01),
        vol.Required(CONF_CAR_MIN_A, default=d.get(CONF_CAR_MIN_A, DEFAULT_CAR_MIN_A)): _num(1, 32, 1, "A"),
        vol.Required(CONF_CAR_MAX_A, default=d.get(CONF_CAR_MAX_A, DEFAULT_CAR_MAX_A)): _num(1, 80, 1, "A"),
        vol.Required(CONF_CAR_PHASES, default=d.get(CONF_CAR_PHASES, DEFAULT_CAR_PHASES)): _num(1, 3, 1),
        vol.Required(CONF_CAR_VOLTAGE, default=d.get(CONF_CAR_VOLTAGE, DEFAULT_CAR_VOLTAGE)): _num(100, 260, 1, "V"),
        vol.Required(CONF_CAR_ALLOW_GRID, default=d.get(CONF_CAR_ALLOW_GRID, True)): bool,
        vol.Required(CONF_CAR_MAX_COMMANDS, default=d.get(CONF_CAR_MAX_COMMANDS, 0)): _num(0, 500, 1),
        vol.Optional(CONF_CAR_PAUSE_ENTITY, **_d(d, CONF_CAR_PAUSE_ENTITY)): sel.EntitySelector(),
        vol.Optional(CONF_CAR_PAUSE_STATE, **_d(d, CONF_CAR_PAUSE_STATE)): str,
    })


def device_schema(d: dict, kind: str, others: list[dict], next_prio: int) -> vol.Schema:
    fields: dict = {vol.Required(CONF_DEV_NAME, **_d(d, CONF_DEV_NAME)): str}
    if kind == KIND_CLIMATE:
        fields[vol.Required(CONF_DEV_ENTITY, **_d(d, CONF_DEV_ENTITY))] = _ent("climate")
        fields[vol.Required(CONF_DEV_CLIMATE_MODE, default=d.get(CONF_DEV_CLIMATE_MODE, "heat"))] = \
            sel.SelectSelector(sel.SelectSelectorConfig(options=CLIMATE_MODES))
    else:
        fields[vol.Required(CONF_DEV_ENTITY, **_d(d, CONF_DEV_ENTITY))] = _ent(["switch", "input_boolean"])
    fields.update({
        vol.Required(CONF_DEV_PRIORITY, default=d.get(CONF_DEV_PRIORITY, next_prio)): _num(1, 99, 1),
        vol.Required(CONF_DEV_POWER_KW, default=d.get(CONF_DEV_POWER_KW, 0.5)): _num(0.01, 30, 0.01, "kW"),
        vol.Optional(CONF_DEV_POWER_SENSOR, **_d(d, CONF_DEV_POWER_SENSOR)): _ent("sensor"),
        vol.Optional(CONF_DEV_SCHEDULE, **_d(d, CONF_DEV_SCHEDULE)): _ent("schedule"),
        vol.Optional(CONF_DEV_WINDOW_START, **_d(d, CONF_DEV_WINDOW_START)): sel.TimeSelector(),
        vol.Optional(CONF_DEV_WINDOW_END, **_d(d, CONF_DEV_WINDOW_END)): sel.TimeSelector(),
        vol.Optional(CONF_DEV_MIN_RUNTIME_H, **_d(d, CONF_DEV_MIN_RUNTIME_H)): _num(0, 24, 0.25, "h"),
        vol.Optional(CONF_DEV_SOC_RESERVE, **_d(d, CONF_DEV_SOC_RESERVE)): _num(0, 100, 1, "%"),
    })
    if others:
        fields[vol.Optional(CONF_DEV_DEPENDS_ON, **_d(d, CONF_DEV_DEPENDS_ON))] = sel.SelectSelector(
            sel.SelectSelectorConfig(options=[{"value": o[CONF_DEV_ID], "label": o[CONF_DEV_NAME]} for o in others],
                                     mode=sel.SelectSelectorMode.DROPDOWN))
    return vol.Schema(fields)


class PilotConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Surplus Pilot", data={**user_input, CONF_DEVICES: []})
        return self.async_show_form(step_id="user", data_schema=energy_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PilotOptionsFlow()


class PilotOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._kind = KIND_SWITCH
        self._edit_id: str | None = None

    @property
    def _data(self) -> dict:
        return dict(self.config_entry.data)

    def _save(self, data: dict):
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(data={})

    async def async_step_init(self, user_input=None):
        menu = ["energy", "car", "device_add"]
        if self._data.get(CONF_DEVICES):
            menu += ["device_edit", "device_remove"]
        if self._data.get(CONF_CAR):
            menu.append("car_remove")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_energy(self, user_input=None):
        if user_input is not None:
            data = self._data
            for k in (CONF_GRID_EXPORT_SENSOR, CONF_BATTERY_SOC_SENSOR, CONF_TIBBER_HOME, CONF_PRICE_SENSOR,
                      CONF_PRICE_HISTORY_SENSOR):
                data.pop(k, None)
            data.update(user_input)
            return self._save(data)
        return self.async_show_form(step_id="energy", data_schema=energy_schema(self._data))

    async def async_step_car(self, user_input=None):
        if user_input is not None:
            data = self._data
            data[CONF_CAR] = user_input
            return self._save(data)
        return self.async_show_form(step_id="car", data_schema=car_schema(self._data.get(CONF_CAR) or {}))

    async def async_step_car_remove(self, user_input=None):
        if user_input is not None:
            data = self._data
            if user_input.get("confirm"):
                data.pop(CONF_CAR, None)
            return self._save(data)
        return self.async_show_form(step_id="car_remove", data_schema=vol.Schema({vol.Required("confirm", default=False): bool}))

    async def async_step_device_add(self, user_input=None):
        if user_input is not None:
            self._kind = user_input[CONF_DEV_KIND]
            self._edit_id = None
            return await self.async_step_device()
        return self.async_show_form(step_id="device_add", data_schema=vol.Schema({
            vol.Required(CONF_DEV_KIND, default=KIND_SWITCH): sel.SelectSelector(sel.SelectSelectorConfig(
                options=[KIND_SWITCH, KIND_CLIMATE], translation_key="device_kind"))}))

    async def async_step_device_edit(self, user_input=None):
        devs = self._data.get(CONF_DEVICES) or []
        if user_input is not None:
            self._edit_id = user_input[CONF_DEV_ID]
            dev = next(d for d in devs if d[CONF_DEV_ID] == self._edit_id)
            self._kind = dev.get(CONF_DEV_KIND, KIND_SWITCH)
            return await self.async_step_device()
        return self.async_show_form(step_id="device_edit", data_schema=vol.Schema({
            vol.Required(CONF_DEV_ID): sel.SelectSelector(sel.SelectSelectorConfig(
                options=[{"value": d[CONF_DEV_ID], "label": d[CONF_DEV_NAME]} for d in devs]))}))

    async def async_step_device(self, user_input=None):
        data = self._data
        devs = list(data.get(CONF_DEVICES) or [])
        current = next((d for d in devs if d[CONF_DEV_ID] == self._edit_id), {}) if self._edit_id else {}
        others = [d for d in devs if d[CONF_DEV_ID] != self._edit_id]
        if user_input is not None:
            dev = {CONF_DEV_ID: self._edit_id or uuid.uuid4().hex[:8], CONF_DEV_KIND: self._kind, **user_input}
            devs = [d for d in devs if d[CONF_DEV_ID] != dev[CONF_DEV_ID]] + [dev]
            devs.sort(key=lambda d: d.get(CONF_DEV_PRIORITY, 50))
            data[CONF_DEVICES] = devs
            return self._save(data)
        next_prio = max([int(d.get(CONF_DEV_PRIORITY, 0)) for d in devs] + [0]) + 1
        return self.async_show_form(step_id="device", data_schema=device_schema(current, self._kind, others, next_prio))

    async def async_step_device_remove(self, user_input=None):
        devs = self._data.get(CONF_DEVICES) or []
        if user_input is not None:
            data = self._data
            data[CONF_DEVICES] = [d for d in devs if d[CONF_DEV_ID] != user_input[CONF_DEV_ID]]
            return self._save(data)
        return self.async_show_form(step_id="device_remove", data_schema=vol.Schema({
            vol.Required(CONF_DEV_ID): sel.SelectSelector(sel.SelectSelectorConfig(
                options=[{"value": d[CONF_DEV_ID], "label": d[CONF_DEV_NAME]} for d in devs]))}))
