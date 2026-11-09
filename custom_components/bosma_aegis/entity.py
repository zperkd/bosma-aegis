"""Shared base entity for Bosma Aegis."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BosmaCoordinator


class BosmaEntity(CoordinatorEntity[BosmaCoordinator]):
    """Base: one HA device per lock, availability from per-device observation freshness."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator)
        self._pid = device["devicePid"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._pid)},
            name=device["name"],
            manufacturer="Bosma",
            model="Aegis",
        )

    @property
    def _obs(self) -> dict:
        return self.coordinator.data.get(self._pid) or {}

    @property
    def available(self) -> bool:
        # Fresh if EITHER the websocket push or the REST poll observed this lock recently; independent of the
        # coordinator's last poll result, so a momentarily failed poll with a live websocket stays available.
        return self.coordinator.available(self._pid)
