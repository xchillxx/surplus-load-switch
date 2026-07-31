"""Actuates a managed device regardless of whether it's controlled via a
switch entity or a climate entity (e.g. a pool heat pump with no on/off
switch, only a thermostat-style mode selector).

Shared between the coordinator (automatic switching) and the per-device
switch entity (manual override / dashboard control), so both actuate a
climate-controlled device identically.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_CLIMATE_ENTITY,
    CONF_DEVICE_CLIMATE_ON_MODE,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SWITCH,
    DOMAIN,
)


def hub_device_info(entry_id: str) -> dict:
    """The single 'SLS' device that global (non-per-device) entities
    belong to. Abbreviated (not the full "Surplus Load Switch") because
    it prefixes every entity's friendly_name via has_entity_name — the
    full name made every global entity's display name unnecessarily
    long. Per-device entities live on their own sub_device_info device
    instead, named after the device itself, not this hub."""
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": "SLS",
    }


def sub_device_info(entry_id: str, device: dict) -> dict:
    """Each configured device (Miner, Boiler, ...) gets its own HA device,
    nested under the hub device via via_device, instead of every device's
    entities piling into one flat list on the hub. Settings -> Devices &
    Services then shows one clean card per configured device, each with
    its own Steuerung section containing only that device's entities."""
    device_id = device["_id"]
    name = device.get(CONF_DEVICE_NAME, device_id)
    return {
        "identifiers": {(DOMAIN, f"{entry_id}_{device_id}")},
        "name": name,
        "via_device": (DOMAIN, entry_id),
    }


def is_device_on(hass: HomeAssistant, dev: dict) -> bool:
    switch_id = dev.get(CONF_DEVICE_SWITCH)
    if switch_id:
        state = hass.states.get(switch_id)
        return state is not None and state.state == "on"

    climate_id = dev.get(CONF_DEVICE_CLIMATE_ENTITY)
    if climate_id:
        state = hass.states.get(climate_id)
        return state is not None and state.state not in ("off", "unavailable", "unknown")

    return False


def control_entity_id(dev: dict) -> str | None:
    """The entity actually being actuated, for logging/attribution."""
    return dev.get(CONF_DEVICE_SWITCH) or dev.get(CONF_DEVICE_CLIMATE_ENTITY)


async def async_turn_on(hass: HomeAssistant, dev: dict) -> None:
    switch_id = dev.get(CONF_DEVICE_SWITCH)
    if switch_id:
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=False)
        return

    climate_id = dev.get(CONF_DEVICE_CLIMATE_ENTITY)
    if climate_id:
        on_mode = dev.get(CONF_DEVICE_CLIMATE_ON_MODE, "heat")
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": on_mode}, blocking=False
        )


async def async_turn_off(hass: HomeAssistant, dev: dict) -> None:
    switch_id = dev.get(CONF_DEVICE_SWITCH)
    if switch_id:
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=False)
        return

    climate_id = dev.get(CONF_DEVICE_CLIMATE_ENTITY)
    if climate_id:
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "off"}, blocking=False
        )
