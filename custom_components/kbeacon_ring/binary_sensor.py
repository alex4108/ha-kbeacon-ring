"""Binary sensor: is the KBeacon tag actively connected (for button events)?

When the button-event platform holds an authenticated BLE connection to the
tag, this sensor is ``on`` (connected). Because a KBeacon stops advertising
connectably while a central is connected, passive trackers (Bermuda/ibeacon)
go blind during that time — but the connection itself proves the tag is in
radio range. So this sensor is a valid presence input: connected => in range
=> home, even when Bermuda sees nothing.

Pair it with Bermuda in a template/automation:
    home  if  Bermuda sees tag  OR  this sensor is on
"""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAC, CONF_NAME, DOMAIN, SIGNAL_CONN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    cfg = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KBeaconConnectedSensor(hass, entry, cfg)])


class KBeaconConnectedSensor(BinarySensorEntity):
    """On while the integration holds a live BLE connection to the tag."""

    _attr_has_entity_name = True
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_entity_category = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cfg: dict) -> None:
        self.hass = hass
        self._mac: str = cfg[CONF_MAC]
        self._name: str = cfg[CONF_NAME]
        self._attr_unique_id = "%s_connected" % self._mac.replace(":", "").lower()
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self._name,
            manufacturer="Blue Charm Beacons",
            model="KBeacon (BCPro)",
        )
        # Default off until the event platform reports a live connection.
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONN % self._mac, self._on_conn
            )
        )

    @callback
    def _on_conn(self, connected: bool) -> None:
        self._attr_is_on = bool(connected)
        self.async_write_ha_state()
