"""Operating mode: automatic / observe only / off."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODES
from .entity import PilotEntity, hub_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([ModeSelect(hass.data[DOMAIN][entry.entry_id], hub_info(entry.entry_id))])


class ModeSelect(PilotEntity, SelectEntity):
    _attr_translation_key = "betriebsart"
    _attr_options = MODES
    _attr_icon = "mdi:auto-mode"

    def __init__(self, co, info):
        super().__init__(co, "betriebsart", info)

    @property
    def current_option(self):
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        self.coordinator.store.data["mode"] = option
        self.coordinator.store.save()
        self.coordinator._log(f"Betriebsart: {option}")
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
