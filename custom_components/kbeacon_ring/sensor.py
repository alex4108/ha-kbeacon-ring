"""Battery sensor for a KBeacon tag.

Reports battery percent (``btPt`` from the device CommonPara block), read over
the persistent connection the button-event platform holds and broadcast via the
SIGNAL_BATTERY dispatcher signal. ``state_class`` measurement + device_class
battery means HA records history and the Battery Notes / low-battery flows pick
it up automatically.

If button events (and thus the held connection) are disabled, this sensor has
no source and stays ``unknown`` — acceptable, since battery is only readable
over a connection on this firmware path.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_MAC, CONF_NAME, DOMAIN, SIGNAL_BATTERY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    cfg = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KBeaconBatterySensor(hass, entry, cfg)])


class KBeaconBatterySensor(SensorEntity, RestoreEntity):
    """Battery percentage of the KBeacon tag (read over the held connection)."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cfg: dict) -> None:
        self.hass = hass
        self._mac: str = cfg[CONF_MAC]
        self._name: str = cfg[CONF_NAME]
        self._attr_unique_id = "%s_battery" % self._mac.replace(":", "").lower()
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self._name,
            manufacturer="Blue Charm Beacons",
            model="KBeacon (BC021)",
        )
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        # Restore last known value across restarts so history/Battery Notes have
        # a value before the next connection read.
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable", None):
            try:
                self._attr_native_value = int(float(last.state))
            except (TypeError, ValueError):
                self._attr_native_value = None
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_BATTERY % self._mac, self._on_battery
            )
        )

    @callback
    def _on_battery(self, percent: int) -> None:
        self._attr_native_value = int(percent)
        self.async_write_ha_state()
