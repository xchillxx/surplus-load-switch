"""A single always-on diagnostic entity purely so system-level logbook
entries (recalibration runs, skipped cycles, anything that isn't a
per-device decision) have somewhere to attach — Home Assistant's logbook
UI needs an entity_id to file an entry under, and the "sensor" domain is
silently dropped from the logbook display regardless of how the entry was
created, so this can't just be another sensor.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PVSurplusCoordinator
from .device_control import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PVSurplusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        PVSystemStatusBinarySensor(coordinator, entry),
        PVBattOkBinarySensor(coordinator, entry),
        PVWeakDayBinarySensor(coordinator, entry),
    ])


class PVSystemStatusBinarySensor(CoordinatorEntity[PVSurplusCoordinator], BinarySensorEntity):
    """Always on while the integration is loaded — its own state carries no
    information, it exists to be the logbook's attribution target for
    system-level messages (see coordinator._log_system)."""

    _attr_has_entity_name = True
    _attr_name = "System"
    _attr_icon = "mdi:cog-sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_system_status"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return True


class PVBattOkBinarySensor(CoordinatorEntity[PVSurplusCoordinator], BinarySensorEntity):
    """Whether the battery is currently projected to last until solar start
    (the same batt_ok flag the Modus sensor's text is derived from) — its
    own entity, not just an attribute on Überschuss, so it gets its own
    history timeline instead of always opening Überschuss's graph."""

    _attr_has_entity_name = True
    _attr_name = "Akku ausreichend"
    _attr_icon = "mdi:battery-check"

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_batt_ok"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.batt_ok if self.coordinator.data else False


class PVWeakDayBinarySensor(CoordinatorEntity[PVSurplusCoordinator], BinarySensorEntity):
    """Whether today counts as a weak-production day compared to the
    calibrated normal for this time of year — see coordinator._async_
    update_data and the WEAK_DAY_* constants. Off (and available) even
    with no reference SOC gain calibrated yet — the attributes explain
    why."""

    _attr_has_entity_name = True
    _attr_name = "Schwacher Tag"
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: PVSurplusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_weak_day"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.is_weak_day if self.coordinator.data else False

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        d = self.coordinator.data
        return {
            "soc_zuwachs_heute": round(d.soc_gain_today, 1) if d.soc_gain_today is not None else None,
            "bester_soc_zuwachs_heute": round(d.peak_soc_gain_today, 1),
            "referenz_soc_zuwachs": round(d.reference_soc_gain, 1) if d.reference_soc_gain else None,
        }
