"""Binary sensors for Bosma Aegis (read-only): door contact and jam."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BosmaCoordinator
from .entity import BosmaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BosmaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []
    for device in coordinator.devices:
        entities.append(BosmaJamSensor(coordinator, device))
        # Door contact only for locks that actually report a door sensor.
        if (coordinator.data.get(device["devicePid"]) or {}).get("door") is not None:
            entities.append(BosmaDoorSensor(coordinator, device))
    async_add_entities(entities)


class BosmaJamSensor(BosmaEntity, BinarySensorEntity):
    """on = jammed."""

    _attr_translation_key = "jammed"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_jammed"

    @property
    def is_on(self) -> bool | None:
        status = self._obs.get("status")
        return status == "jammed" if status is not None else None


class BosmaDoorSensor(BosmaEntity, BinarySensorEntity):
    """on = door open (for locks with a door sensor)."""

    _attr_translation_key = "door"
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_door"

    @property
    def is_on(self) -> bool | None:
        door = self._obs.get("door")
        if door is None:
            return None
        return door == "open"
