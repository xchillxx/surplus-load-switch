"""Surplus Pilot — one plan for PV surplus: car, home battery and devices."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import PilotCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PilotCoordinator(hass, entry)
    await coordinator.async_setup()
    # async_refresh, not async_config_entry_first_refresh: a sensor that is
    # briefly unavailable at startup must not strand the whole integration.
    await coordinator.async_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    unsub = coordinator.async_watch_car()
    if unsub:
        entry.async_on_unload(unsub)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: PilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.store.async_save_now()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok
