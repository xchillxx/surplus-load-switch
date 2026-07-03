"""Surplus Load Switch — Home Assistant custom integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICES, CONF_MIN_SOC, CONF_SOLAR_OFFSETS, DEFAULT_SOLAR_OFFSETS, DOMAIN, PLATFORMS
from .coordinator import PVSurplusCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = {**entry.data}
    config.setdefault(CONF_SOLAR_OFFSETS, DEFAULT_SOLAR_OFFSETS)
    config.setdefault(CONF_MIN_SOC, 20.0)
    config.setdefault(CONF_DEVICES, [])

    coordinator = PVSurplusCoordinator(hass, config, entry.entry_id)
    await coordinator.async_setup_power_trackers()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
