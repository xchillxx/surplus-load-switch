"""Config flow: initial setup (with immediate device add) + options (manage devices later)."""
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


def _global_settings_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_SOLAR_SENSOR, default=d.get(CONF_SOLAR_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_LOAD_SENSOR, default=d.get(CONF_LOAD_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_SOC_SENSOR, default=d.get(CONF_SOC_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="battery")
        ),
        vol.Required(CONF_BATT_SENSOR, default=d.get(CONF_BATT_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_BATTERY_CAPACITY_KWH, default=d.get(CONF_BATTERY_CAPACITY_KWH, 13.8)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=100, step=0.1, unit_of_measurement="kWh")
        ),
        vol.Required(CONF_MIN_SOC, default=d.get(CONF_MIN_SOC, 20)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=50, step=1, unit_of_measurement="%")
        ),
    })


def _device_schema(defaults: dict | None = None, next_priority: int = 1) -> vol.Schema:
    """Schema for add/edit device. `defaults` pre-fills values when editing.

    switch_entity is Optional here even though non-wallbox devices need one —
    a wallbox controlled by its own inverter logic usually has no HA switch at
    all. The step handler enforces "required unless wallbox" manually.
    """
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_DEVICE_NAME, default=d.get(CONF_DEVICE_NAME)): str,
        vol.Optional(CONF_DEVICE_SWITCH, default=d.get(CONF_DEVICE_SWITCH)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_DEVICE_PRIORITY, default=d.get(CONF_DEVICE_PRIORITY, next_priority)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=99, step=1)
        ),
        vol.Optional(CONF_DEVICE_POWER_SENSOR, default=d.get(CONF_DEVICE_POWER_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        ),
        vol.Required(CONF_DEVICE_POWER_KW, default=d.get(CONF_DEVICE_POWER_KW, 0.15)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.05, max=22.0, step=0.05, unit_of_measurement="kW")
        ),
        vol.Optional(CONF_DEVICE_IS_WALLBOX, default=d.get(CONF_DEVICE_IS_WALLBOX, False)): bool,
    })


def _validate_device_input(user_input: dict) -> dict[str, str]:
    """Returns an errors dict (empty if valid). A switch is required unless
    the device is a wallbox (those are only ever read, never switched)."""
    errors: dict[str, str] = {}
    if not user_input.get(CONF_DEVICE_IS_WALLBOX) and not user_input.get(CONF_DEVICE_SWITCH):
        errors[CONF_DEVICE_SWITCH] = "switch_required"
    return errors


def _next_priority(devices: list[dict]) -> int:
    return max((d.get(CONF_DEVICE_PRIORITY, 0) for d in devices), default=0) + 1


class PVSurplusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: global sensors, then loop to add devices right away."""

    VERSION = 1

    def __init__(self) -> None:
        self._global_data: dict = {}
        self._devices: list[dict] = []

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._global_data = user_input
            return await self.async_step_device_intro()

        return self.async_show_form(
            step_id="user",
            data_schema=_global_settings_schema(),
            errors=errors,
        )

    async def async_step_device_intro(self, user_input: dict | None = None):
        """First chance to add a device right after the base setup."""
        return self.async_show_menu(
            step_id="device_intro",
            menu_options=["add_device", "finish_setup"],
        )

    async def async_step_add_device(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device_input(user_input)
            if not errors:
                self._devices.append({**user_input, "_id": str(uuid.uuid4())})
                return await self.async_step_add_another()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_device_schema(next_priority=_next_priority(self._devices)),
            errors=errors,
            description_placeholders={"count": str(len(self._devices))},
        )

    async def async_step_add_another(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="add_another",
            menu_options=["add_device", "finish_setup"],
        )

    async def async_step_finish_setup(self, user_input: dict | None = None):
        return self.async_create_entry(
            title="PV Surplus Manager",
            data={**self._global_data, CONF_DEVICES: self._devices},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PVSurplusOptionsFlow(config_entry)


class PVSurplusOptionsFlow(OptionsFlow):
    """Options flow: manage devices and global settings after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._devices: list[dict] = list(config_entry.data.get(CONF_DEVICES, []))
        self._edit_target: str | None = None  # holds a device's _id while editing

    def _save_devices(self) -> None:
        new_data = {**self._config_entry.data, CONF_DEVICES: self._devices}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)

    @staticmethod
    def _device_label(d: dict) -> str:
        kind = " (Wallbox)" if d.get(CONF_DEVICE_IS_WALLBOX) else ""
        return f"{d.get(CONF_DEVICE_NAME)} (Prio {d.get(CONF_DEVICE_PRIORITY)}){kind}"

    async def async_step_init(self, user_input: dict | None = None):
        menu_options = ["add_device"]
        if self._devices:
            menu_options += ["edit_device", "remove_device"]
        menu_options += ["global_settings", "finish"]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_global_settings(self, user_input: dict | None = None):
        if user_input is not None:
            new_data = {**self._config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return await self.async_step_init()

        return self.async_show_form(
            step_id="global_settings",
            data_schema=_global_settings_schema(self._config_entry.data),
        )

    async def async_step_add_device(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device_input(user_input)
            if not errors:
                self._devices.append({**user_input, "_id": str(uuid.uuid4())})
                self._save_devices()
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_device",
            data_schema=_device_schema(next_priority=_next_priority(self._devices)),
            errors=errors,
            description_placeholders={"count": str(len(self._devices))},
        )

    async def async_step_edit_device(self, user_input: dict | None = None):
        if not self._devices:
            return await self.async_step_init()

        if self._edit_target is None:
            if user_input is not None:
                self._edit_target = user_input["device"]
                return await self.async_step_edit_device()

            options = {d["_id"]: self._device_label(d) for d in self._devices}
            return self.async_show_form(
                step_id="edit_device",
                data_schema=vol.Schema({vol.Required("device"): vol.In(options)}),
            )

        current = next((d for d in self._devices if d.get("_id") == self._edit_target), None)
        if current is None:
            self._edit_target = None
            return await self.async_step_init()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_device_input(user_input)
            if not errors:
                target_id = self._edit_target
                self._devices = [
                    {**user_input, "_id": target_id} if d.get("_id") == target_id else d
                    for d in self._devices
                ]
                self._edit_target = None
                self._save_devices()
                return await self.async_step_init()

        return self.async_show_form(
            step_id="edit_device",
            data_schema=_device_schema(defaults=current),
            errors=errors,
            description_placeholders={"count": str(len(self._devices))},
        )

    async def async_step_remove_device(self, user_input: dict | None = None):
        if not self._devices:
            return await self.async_step_init()

        if user_input is not None:
            target = user_input["device"]
            self._devices = [d for d in self._devices if d.get("_id") != target]
            self._save_devices()
            return await self.async_step_init()

        options = {d["_id"]: self._device_label(d) for d in self._devices}
        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema({vol.Required("device"): vol.In(options)}),
        )

    async def async_step_finish(self, user_input: dict | None = None):
        return self.async_create_entry(title="", data={})
