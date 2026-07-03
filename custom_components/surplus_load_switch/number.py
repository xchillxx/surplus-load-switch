"""Number entity: configurable min SOC."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MIN_SOC, DOMAIN
from .coordinator import PVSurplusCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PVMinSocNumber(coordinator, entry)])


class PVMinSocNumber(CoordinatorEntity[PVSurplusCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Mindest-SOC"
    _attr_icon = "mdi:battery-low"
    _attr_native_min_value = 5
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_min_soc"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Surplus Load Switch",
        }

    @property
    def native_value(self) -> float:
        return self._entry.data.get(CONF_MIN_SOC, 20.0)

    async def async_set_native_value(self, value: float) -> None:
        new_data = {**self._entry.data, CONF_MIN_SOC: value}
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        # Update coordinator config live
        self.coordinator._config[CONF_MIN_SOC] = value
        await self.coordinator.async_request_refresh()
