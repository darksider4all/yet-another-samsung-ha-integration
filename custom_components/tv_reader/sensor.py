"""Diagnostic sensors for the Samsung TV Reader integration.

Hardware/system data pulled from the TV via the on-TV agent (tizen.systeminfo /
webapis.avinfo): free memory, uptime, eco (ambient-light) sensor, energy saving
and broadcast standard. All grouped under the same "Samsung TV" device as the
media_player. (Power + running app are now on the media_player entity.)
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import TvReaderCoordinator

_DEVICE = {
    "identifiers": {(DOMAIN, "samsung-tv")},
    "name": "Samsung TV",
    "manufacturer": "Samsung",
    "model": "UE55DU8572UXXH",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c: TvReaderCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TvFreeMemory(c),
            TvUptime(c),
            TvEcoSensor(c),
            TvEnergySaving(c),
            TvBroadcastStandard(c),
        ]
    )


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TvReaderCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = _DEVICE

    @property
    def _sensors(self) -> dict:
        return (((self.coordinator.data or {}).get("agent") or {}).get("sensors")) or {}

    @staticmethod
    def _clean(v):
        if isinstance(v, str) and v.startswith("err"):
            return None
        return v


class TvFreeMemory(_Base):
    _attr_name = "Free memory"
    _attr_unique_id = "samsung_tv_free_memory"
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_icon = "mdi:memory"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        v = self._sensors.get("freeMemMB")
        return v if isinstance(v, (int, float)) else None


class TvUptime(_Base):
    _attr_name = "Uptime"
    _attr_unique_id = "samsung_tv_uptime"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self):
        v = self._sensors.get("uptimeSec")
        return v if isinstance(v, (int, float)) else None


class TvEcoSensor(_Base):
    _attr_name = "Eco sensor"
    _attr_unique_id = "samsung_tv_eco_sensor"
    _attr_icon = "mdi:brightness-auto"

    @property
    def native_value(self):
        return self._clean(self._sensors.get("ecoSensor"))


class TvEnergySaving(_Base):
    _attr_name = "Energy saving"
    _attr_unique_id = "samsung_tv_energy_saving"
    _attr_icon = "mdi:leaf"

    @property
    def native_value(self):
        return self._clean(self._sensors.get("energySaving"))


class TvBroadcastStandard(_Base):
    _attr_name = "Broadcast standard"
    _attr_unique_id = "samsung_tv_broadcast_standard"
    _attr_icon = "mdi:broadcast"

    @property
    def native_value(self):
        return self._clean(self._sensors.get("broadcastStd"))

