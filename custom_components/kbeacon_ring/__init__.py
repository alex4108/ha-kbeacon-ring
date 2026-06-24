"""The KBeacon Ring (Blue Charm) integration."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_MAC, DOMAIN
from .coordinator import KBeaconCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up KBeacon Ring from a config entry."""
    mac: str = entry.data[CONF_MAC]

    # Verify HA currently has a (preferably connectable) route to the beacon.
    # We don't hard-fail on connectable here because the tag may be momentarily
    # out of range; the button press re-resolves at call time.
    if not bluetooth.async_address_present(hass, mac, connectable=True):
        _LOGGER.warning(
            "kbeacon_ring: no connectable BLE route to %s right now; "
            "ring will retry at press time",
            mac,
        )

    hass.data.setdefault(DOMAIN, {})
    cfg = dict(entry.data)
    # Shared single-BLE-slot coordinator (listener yields to ring commands).
    cfg["coordinator"] = KBeaconCoordinator()
    hass.data[DOMAIN][entry.entry_id] = cfg

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
