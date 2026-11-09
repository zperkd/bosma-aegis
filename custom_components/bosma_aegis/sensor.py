"""Sensor entities for Bosma Aegis (read-only): lock status, battery, last activity."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BosmaCoordinator
from .entity import BosmaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BosmaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for device in coordinator.devices:
        entities.append(BosmaStatusSensor(coordinator, device))
        entities.append(BosmaBatterySensor(coordinator, device))
        entities.append(BosmaActivitySensor(coordinator, device))
    async_add_entities(entities)


class BosmaStatusSensor(BosmaEntity, SensorEntity):
    """locked / unlocked / jammed."""

    _attr_translation_key = "lock_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["locked", "unlocked", "jammed"]
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_status"

    @property
    def native_value(self) -> str | None:
        return self._obs.get("status")

    @property
    def extra_state_attributes(self) -> dict:
        # `observed_at` (epoch) is when this state was last observed (push or poll); downstream templates use it.
        return {"observed_at": self._obs.get("observed_at")}


class BosmaBatterySensor(BosmaEntity, SensorEntity):
    """Battery percentage."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_battery"

    @property
    def native_value(self) -> int | None:
        value = self._obs.get("battery")
        return int(value) if value is not None else None


class BosmaActivitySensor(BosmaEntity, SensorEntity):
    """Last activity (how it was locked/unlocked). State = the action name; who/person/correlation in attributes.

    `person` is True only when the activity was confidently correlated to a transition (a human lock/unlock);
    None when it couldn't be confirmed; False for automatic/alarm activity. `attribution` reflects that.
    """

    _attr_translation_key = "last_activity"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: BosmaCoordinator, device: dict[str, str]) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._pid}_last_activity"

    @property
    def native_value(self) -> str | None:
        activity = self._obs.get("activity")
        return activity.get("act_name") if activity else None

    @property
    def extra_state_attributes(self) -> dict:
        activity = self._obs.get("activity") or {}
        return {
            "who": activity.get("who"),
            "act_type": activity.get("act_type"),
            "person": activity.get("person"),
            "correlated": activity.get("correlated"),
            "attribution": "correlated" if activity.get("correlated") else "historical_unconfirmed",
        }
