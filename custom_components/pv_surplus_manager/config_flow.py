"""Config flow: initial setup + options (add/remove/edit devices)."""
from __future__ import annotations

import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATT_SENSOR,
    CONF_DEVICES,
    CONF_DEVICE_IS_WALLBOX,
    CONF_DEVICE_NAME,
    CONF_DEVICE_POWER_KW,
    CONF_DEVICE_POWER_SENSOR,
    CONF_DEVICE_PRIORITY,
    CONF_DEVICE_SWITCH,
    CONF_LOAD_SENSOR,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    CONF_SOLAR_SENSOR,
    DOMAIN,
)


class PVSurplusConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            return self.async_create_entry(
                title="PV Surplus Manager",
                data={**user_input, CONF_DEVICES: []},
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_SOLAR_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_LOAD_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_SOC_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="battery")
                ),
                vol.Required(CONF_BATT_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_BATTERY_CAPACITY_KWH, default=13.8): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=100, step=0.1, unit_of_measurement="kWh")
                ),
                vol.Required(CONF_MIN_SOC, default=20): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=50, step=1, unit_of_measurement="%")
                ),
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PVSurplusOptionsFlow(config_entry)


class PVSurplusOptionsFlow(OptionsFlow):
    """Options flow: manage devices and global settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._devices: list[dict] = list(config_entry.data.get(CONF_DEVICES, []))

    async def async_step_init(self, user_input: dict | None = None):
        """Main menu: add/remove device, edit global settings, or finish."""
        menu_options = ["add_device"]
        if self._devices:
            menu_options.append("remove_device")
        menu_options += ["global_settings", "finish"]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_global_settings(self, user_input: dict | None = None):
        data = self._config_entry.data
        if user_input is not None:
            new_data = {**data, **user_input}
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="global_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_SOLAR_SENSOR, default=data.get(CONF_SOLAR_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_LOAD_SENSOR, default=data.get(CONF_LOAD_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_SOC_SENSOR, default=data.get(CONF_SOC_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="battery")
                ),
                vol.Required(CONF_BATT_SENSOR, default=data.get(CONF_BATT_SENSOR)): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_BATTERY_CAPACITY_KWH, default=data.get(CONF_BATTERY_CAPACITY_KWH, 13.8)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=100, step=0.1, unit_of_measurement="kWh")
                ),
                vol.Required(CONF_MIN_SOC, default=data.get(CONF_MIN_SOC, 20)): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=50, step=1, unit_of_measurement="%")
                ),
            }),
        )

    async def async_step_add_device(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            max_prio = max((d.get(CONF_DEVICE_PRIORITY, 0) for d in self._devices), default=0)
            new_device = {
                **user_input,
                CONF_DEVICE_PRIORITY: max_prio + 1,
                "_id": str(uuid.uuid4()),
            }
            self._devices.append(new_device)
            new_data = {**self._config_entry.data, CONF_DEVICES: self._devices}
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_NAME): str,
                vol.Required(CONF_DEVICE_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(CONF_DEVICE_POWER_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                vol.Required(CONF_DEVICE_POWER_KW, default=0.15): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0.05, max=22.0, step=0.05, unit_of_measurement="kW")
                ),
                vol.Optional(CONF_DEVICE_IS_WALLBOX, default=False): bool,
            }),
            errors=errors,
            description_placeholders={"count": str(len(self._devices))},
        )

    async def async_step_remove_device(self, user_input: dict | None = None):
        if not self._devices:
            return await self.async_step_init()

        if user_input is not None:
            target = user_input["device"]
            self._devices = [d for d in self._devices if d[CONF_DEVICE_SWITCH] != target]
            new_data = {**self._config_entry.data, CONF_DEVICES: self._devices}
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return await self.async_step_init()

        options = {
            d[CONF_DEVICE_SWITCH]: f"{d.get(CONF_DEVICE_NAME)} (Prio {d.get(CONF_DEVICE_PRIORITY)})"
            for d in self._devices
        }
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema({vol.Required("device"): vol.In(options)}),
        )

    async def async_step_finish(self, user_input: dict | None = None):
        return self.async_create_entry(title="", data={})
