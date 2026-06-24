"""Event platform for KBeacon Ring (Blue Charm) — physical button presses.

HOW THIS WORKS (from kbeaconlib2, the CURRENT vendor SDK)
---------------------------------------------------------
The KBeacon tag reports button presses as **live GATT indications** on the
``FEA3`` characteristic — a connection-oriented event, NOT an advertisement.
Two requirements:

1. Configure each button gesture with trigger action **Report2App (0x10)** so
   the firmware delivers the press to a connected client (vs Advertisement=1,
   which only broadcasts). See ``KBeaconSession.write_trigger_report2app``.
2. Hold an authenticated connection and subscribe to FEA3 indications. Each
   indication's first byte (``& 0x3F``) is the KBTriggerType gesture:
   3=long/hold, 4=single, 5=double, 6=triple.

This entity therefore keeps a **persistent authenticated BLE connection** to the
tag (occupying one proxy connection slot) and fires ``event.sophie_tag_button``
with single/double/triple/hold when the button is pressed. It reconnects with
backoff if the link drops.
"""
from __future__ import annotations

import asyncio
import logging

from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAC, CONF_NAME, CONF_PASSWORD, DOMAIN, SIGNAL_CONN
from .kbeacon import KBeaconSession

_LOGGER = logging.getLogger(__name__)

# KBTriggerType gesture -> HA event type.
GESTURE_LONG = 3
GESTURE_SINGLE = 4
GESTURE_DOUBLE = 5
GESTURE_TRIPLE = 6
GESTURE_TO_EVENT = {
    GESTURE_SINGLE: "single",
    GESTURE_DOUBLE: "double",
    GESTURE_TRIPLE: "triple",
    GESTURE_LONG: "hold",
}
ALL_GESTURES = [GESTURE_LONG, GESTURE_SINGLE, GESTURE_DOUBLE, GESTURE_TRIPLE]
EVENT_TYPES = ["single", "double", "triple", "hold"]

# Reconnect backoff bounds (seconds).
_RECONNECT_MIN = 5
_RECONNECT_MAX = 120


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    cfg = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KBeaconButtonEvent(hass, entry, cfg)])


class KBeaconButtonEvent(EventEntity):
    """Fires on physical button press via a held FEA3 indication subscription."""

    _attr_has_entity_name = True
    _attr_name = "Button"
    _attr_icon = "mdi:gesture-tap-button"
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cfg: dict) -> None:
        self.hass = hass
        self._cfg = cfg
        self._mac: str = cfg[CONF_MAC]
        self._name: str = cfg[CONF_NAME]
        self._password: str = cfg[CONF_PASSWORD]
        self._attr_unique_id = "%s_button_event" % self._mac.replace(":", "").lower()
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self._name,
            manufacturer="Blue Charm Beacons",
            model="KBeacon (BCPro)",
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client = None

    async def async_added_to_hass(self) -> None:
        self._stop.clear()
        self._task = self.hass.async_create_background_task(
            self._run(), name="kbeacon-button-%s" % self._mac
        )
        self.async_on_remove(self._cleanup)

    @callback
    def _cleanup(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        """Maintain an authed connection + FEA3 subscription, with reconnect."""
        backoff = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                await self._connect_and_listen()
                backoff = _RECONNECT_MIN  # clean exit resets backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.info(
                    "kbeacon button: link to %s lost/failed (%s); retry in %ds",
                    self._mac,
                    exc,
                    backoff,
                )
            finally:
                await self._disconnect()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_MAX)

    @callback
    def _set_connected(self, connected: bool) -> None:
        """Broadcast connection state to the connection binary_sensor."""
        async_dispatcher_send(self.hass, SIGNAL_CONN % self._mac, connected)

    async def _connect_and_listen(self) -> None:
        mac = self._mac
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, mac, connectable=True
        )
        if ble_device is None:
            raise RuntimeError("no connectable BLE route")

        from bleak import BleakClient

        self._client = await establish_connection(
            client_class=BleakClient,
            device=ble_device,
            name="kbeacon-btn-%s" % mac,
            max_attempts=3,
        )
        session = KBeaconSession(self._client, mac, self._password)
        if not await session.authenticate():
            raise RuntimeError("auth failed")

        # Ensure all four gestures report to the app over FEA3.
        try:
            await session.write_trigger_report2app(ALL_GESTURES)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "kbeacon button: trigger config write failed (%s); "
                "subscribing anyway in case it was already armed",
                exc,
            )

        await session.subscribe_button_events(self._on_gesture)
        _LOGGER.info("kbeacon button: %s connected + listening on FEA3", mac)
        self._set_connected(True)

        # Hold the connection until told to stop or the link drops. A
        # disconnect raises through the client; we poll liveness periodically.
        while not self._stop.is_set():
            if self._client is None or not self._client.is_connected:
                raise RuntimeError("disconnected")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    @callback
    def _on_gesture(self, gesture: int, body: bytes) -> None:
        event = GESTURE_TO_EVENT.get(gesture)
        if event is None:
            _LOGGER.info("kbeacon button: unmapped gesture %d (body=%s)", gesture, body.hex())
            return
        _LOGGER.info("kbeacon button: %s -> %s", self._mac, event)
        self._trigger_event(event)
        self.async_write_ha_state()

    async def _disconnect(self) -> None:
        self._set_connected(False)
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
