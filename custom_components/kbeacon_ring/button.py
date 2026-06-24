"""Button platform for KBeacon Ring (Blue Charm).

Exposes two independent commands, both driven by the proven MD5-auth + ring
session in ``kbeacon.py``:

* **Chirp** — audible beep only (ringType=1). This is the mode under active
  proving: it must be *heard*, not merely acked.
* **Blink** — LED flash only (ringType=0). Known-good visually; kept as a
  permanent supported command.

Both buttons share one connect->auth->ring flow; they differ only in the
ring parameters handed to :meth:`KBeaconSession.ring`.
"""
from __future__ import annotations

import logging

from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LED_OFF,
    CONF_LED_ON,
    CONF_MAC,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_RING_MS,
    DEFAULT_LED_OFF,
    DEFAULT_LED_ON,
    DEFAULT_RING_MS,
    DOMAIN,
)
from .kbeacon import KBeaconSession

_LOGGER = logging.getLogger(__name__)

# ringType values understood by the device firmware.
RING_TYPE_LED = 0
RING_TYPE_BEEP = 1
RING_TYPE_LED_BEEP = 2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    cfg = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            KBeaconChirpButton(hass, entry, cfg),
            KBeaconBlinkButton(hass, entry, cfg),
            KBeaconArmTriggerButton(hass, entry, cfg),
        ]
    )


class _KBeaconRingButtonBase(ButtonEntity):
    """Shared connect->auth->ring plumbing for the KBeacon command buttons."""

    _attr_has_entity_name = True

    # Subclasses set these.
    _command_label: str = "Ring"
    _command_slug: str = "ring"
    _ring_type: int = RING_TYPE_LED_BEEP
    _attr_icon = "mdi:bell-ring"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cfg: dict) -> None:
        self.hass = hass
        self._cfg = cfg
        self._mac: str = cfg[CONF_MAC]
        self._name: str = cfg[CONF_NAME]
        self._attr_name = self._command_label
        self._attr_unique_id = "%s_%s" % (
            self._mac.replace(":", "").lower(),
            self._command_slug,
        )
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", self._mac)},
            identifiers={(DOMAIN, self._mac)},
            name=self._name,
            manufacturer="Blue Charm Beacons",
            model="KBeacon (BCPro)",
        )

    def _ring_kwargs(self) -> dict:
        """Per-command ring parameters; overridden by subclasses as needed."""
        ms = int(self._cfg.get(CONF_RING_MS, DEFAULT_RING_MS))
        kwargs = {"ring_ms": ms, "ring_type": self._ring_type}
        if self._ring_type in (RING_TYPE_LED, RING_TYPE_LED_BEEP):
            kwargs["led_on"] = int(self._cfg.get(CONF_LED_ON, DEFAULT_LED_ON))
            kwargs["led_off"] = int(self._cfg.get(CONF_LED_OFF, DEFAULT_LED_OFF))
        return kwargs

    async def async_press(self) -> None:
        mac = self._mac
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, mac, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                "No connectable BLE route to %s (tag out of range of all proxies?)"
                % mac
            )

        _LOGGER.info(
            "kbeacon_ring: %s -> connecting to %s", self._command_slug, mac
        )
        client = await establish_connection(
            client_class=__import__("bleak").BleakClient,
            device=ble_device,
            name="kbeacon-%s" % mac,
            max_attempts=4,
        )
        try:
            session = KBeaconSession(client, mac, self._cfg[CONF_PASSWORD])
            ok = await session.authenticate()
            if not ok:
                raise HomeAssistantError(
                    "KBeacon auth failed for %s (wrong password?)" % mac
                )
            kwargs = self._ring_kwargs()
            _LOGGER.info(
                "kbeacon_ring: %s authed; sending ring %s", self._command_slug, kwargs
            )
            await session.ring(**kwargs)
            _LOGGER.info(
                "kbeacon_ring: %s command delivered to %s", self._command_slug, mac
            )
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


class KBeaconChirpButton(_KBeaconRingButtonBase):
    """Audible beep only — the mode under active proving."""

    _command_label = "Chirp"
    _command_slug = "chirp"
    _ring_type = RING_TYPE_BEEP
    _attr_icon = "mdi:bullhorn"


class KBeaconBlinkButton(_KBeaconRingButtonBase):
    """LED flash only — known-good visual command.

    Overrides timing so the LED visibly *pulses* for several seconds rather
    than firing a single flash: a long ring window with a brisk on/off duty
    cycle. The shared defaults (ledOff=1800ms over a 5s window) produced only
    one visible flash.
    """

    _command_label = "Blink"
    _command_slug = "blink"
    _ring_type = RING_TYPE_LED
    _attr_icon = "mdi:led-on"

    # Blink-specific timing (ms). ~50 pulses over 20s — a sustained strobe.
    _blink_ring_ms = 20000
    _blink_led_on = 150
    _blink_led_off = 250

    def _ring_kwargs(self) -> dict:
        return {
            "ring_ms": self._blink_ring_ms,
            "ring_type": RING_TYPE_LED,
            "led_on": self._blink_led_on,
            "led_off": self._blink_led_off,
        }


# Trigger config constants (from decompiled KBCfgTrigger).
TR_TYPE_BUTTON = 2
TR_ACT_ADV = 1
TR_ACT_ALERT = 2
TR_ACT_ADV_ALERT = TR_ACT_ADV | TR_ACT_ALERT  # broadcast AND flash on press
TR_PARA_HOLD = 1
TR_PARA_SINGLE = 2
TR_PARA_DOUBLE = 4
TR_PARA_TRIPLE = 8
TR_PARA_ALL = TR_PARA_HOLD | TR_PARA_SINGLE | TR_PARA_DOUBLE | TR_PARA_TRIPLE
TR_ADV_TYPE_IBEACON = 4
TR_CFG_STYPE = 64  # KBCfgTrigger.cfgParaType()
CAP_STYPE_COMMON = 32


class KBeaconArmTriggerButton(_KBeaconRingButtonBase):
    """One-shot: configure the tag so a physical button press emits an advert.

    Reads the tag's capability (bCap) + current trigger config first (so we log
    what we are about to change and confirm the button is supported), then
    writes ``button -> Adv`` for all four gestures. After this, a physical press
    broadcasts a burst that ``event.sophie_tag_button`` can catch passively.

    Idempotent: safe to press repeatedly. Lives as a permanent maintenance
    control (re-arm after a factory reset / battery pull).
    """

    _command_label = "Arm Button Trigger"
    _command_slug = "arm_button_trigger"
    _attr_icon = "mdi:button-pointer"
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        mac = self._mac
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, mac, connectable=True
        )
        if ble_device is None:
            raise HomeAssistantError(
                "No connectable BLE route to %s (tag out of range?)" % mac
            )
        client = await establish_connection(
            client_class=__import__("bleak").BleakClient,
            device=ble_device,
            name="kbeacon-%s" % mac,
            max_attempts=4,
        )
        try:
            session = KBeaconSession(client, mac, self._cfg[CONF_PASSWORD])
            if not await session.authenticate():
                raise HomeAssistantError(
                    "KBeacon auth failed for %s (wrong password?)" % mac
                )

            # 1) Capability — confirm the button exists on THIS unit.
            common = await session.read_config(stype=CAP_STYPE_COMMON)
            bcap = (common or {}).get("bCap")
            if bcap is not None:
                bits = int(bcap)
                _LOGGER.info(
                    "kbeacon arm: bCap=%d button=%s beep=%s acc=%s temp=%s",
                    bits,
                    bool(bits & 1),
                    bool(bits & 2),
                    bool(bits & 4),
                    bool(bits & 8),
                )
                if not (bits & 1):
                    raise HomeAssistantError(
                        "Tag %s reports no button capability (bCap=%d)"
                        % (mac, bits)
                    )

            # 2) Current trigger config — log so we don't silently clobber.
            # The SDK's readTriggerConfig REQUIRES trType in the request; without
            # it the device only sends a 0x23..0103 ack and the read times out.
            before = await session.read_config(
                stype=TR_CFG_STYPE, tr_type=TR_TYPE_BUTTON
            )
            _LOGGER.info("kbeacon arm: trigger config BEFORE = %s", before)

            # 3) Arm: button -> Adv+Alert (flash AND broadcast), all gestures,
            #    long 30s iBeacon burst so the passive proxy can't miss it.
            params = {
                "stype": TR_CFG_STYPE,
                "trType": TR_TYPE_BUTTON,
                "trAct": TR_ACT_ADV_ALERT,
                "trPara": TR_PARA_ALL,
                "trAType": TR_ADV_TYPE_IBEACON,
                "trATm": 30,
            }
            await session.write_config(params)

            # 4) Read back to confirm it stuck (with trType, the fixed read).
            after = await session.read_config(
                stype=TR_CFG_STYPE, tr_type=TR_TYPE_BUTTON
            )
            _LOGGER.info("kbeacon arm: trigger config AFTER = %s", after)
            _LOGGER.info(
                "kbeacon arm: %s armed (button->Adv+Alert, 30s). Press the tag "
                "now and watch for 'kbeacon poll CHANGED' INFO lines.",
                mac,
            )
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
