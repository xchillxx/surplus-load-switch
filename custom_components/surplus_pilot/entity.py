"""Shared entity base."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEV_ID, CONF_DEV_NAME, DOMAIN
from .coordinator import PilotCoordinator


def hub_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, entry_id)}, name="Surplus Pilot", manufacturer="Surplus Pilot")


def device_info(entry_id: str, dev: dict) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, f"{entry_id}_{dev[CONF_DEV_ID]}")}, name=dev.get(CONF_DEV_NAME),
                      manufacturer="Surplus Pilot", via_device=(DOMAIN, entry_id))


def car_info(entry_id: str, name: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, f"{entry_id}_car")}, name=name, manufacturer="Surplus Pilot",
                      via_device=(DOMAIN, entry_id))


def departures_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, f"{entry_id}_departures")}, translation_key="departures",
                      manufacturer="Surplus Pilot", via_device=(DOMAIN, entry_id))


class PilotEntity(CoordinatorEntity[PilotCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PilotCoordinator, key: str, info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = info
