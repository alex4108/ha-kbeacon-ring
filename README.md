# KBeacon Ring (Blue Charm) — Home Assistant integration

A local-push Home Assistant integration that rings **KBeacon / Blue Charm BLE tags**
(e.g. `BCPro` series) over Bluetooth — flashing the LED, sounding the buzzer, or both.
Useful as a "find my tag" button for keys, bags, pets, kids' items, etc.

It connects to the tag through any Home Assistant Bluetooth proxy / local adapter,
performs the KBeacon MD5 challenge–response authentication, and issues the `ring`
command. No cloud, no app — everything stays local.

## Features

### Ring buttons

Each configured tag exposes two **button** entities:

| Entity | Effect | `ringType` |
|--------|--------|-----------|
| **Chirp** (`button.<name>_chirp`) | Audible beep only | `1` |
| **Blink** (`button.<name>_blink`) | LED strobe (~20 s) only | `0` |

The hardware also supports **LED + beep together** (`ringType: 2`) — easy to add as a
third button if desired.

> **Why a sustained connection?** The tag only drives its LED/buzzer *while a BLE
> central stays connected*. The integration therefore holds the connection open for
> the full ring window (`ringTime`) so the effect plays out completely rather than
> producing a single flash/blip.

### Physical button events ✨

If your tag has a physical push-button (e.g. `BCPro`), the integration also exposes
an **event** entity that fires on real button presses:

| Entity | Event types |
|--------|-------------|
| **Button** (`event.<name>_button`) | `single`, `double`, `triple`, `hold` |

Press the tag and `event.<name>_button` fires with the matching `event_type` —
ready to drive any automation (find-my-phone, scene toggle, SOS, etc).

> **How it works.** Button presses are **not** broadcast in advertisements — they
> are delivered as live GATT **indications** on characteristic `FEA3`. The
> integration holds a persistent authenticated connection, configures each gesture
> with trigger action **Report2App**, subscribes to `FEA3`, and decodes
> `gesture = data[0] & 0x3F` (3=hold, 4=single, 5=double, 6=triple). It reconnects
> automatically (5→120 s backoff) if the link drops. This occupies one Bluetooth
> proxy connection slot continuously — the necessary cost of real-time events.

## Installation (HACS)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add `https://github.com/alex4108/ha-kbeacon-ring` with category **Integration**.
3. Install **KBeacon Ring (Blue Charm)** and restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → KBeacon Ring**.

### Manual installation

Copy `custom_components/kbeacon_ring/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

The config flow asks for:

- **MAC address** of the tag (e.g. `DD:88:00:00:1E:3E`)
- **Name** (used for entity ids / friendly names)
- **Password** — the KBeacon access password. Factory default is sixteen zeros
  (`0000000000000000`).

The tag must be within range of a Home Assistant Bluetooth adapter or
[Bluetooth proxy](https://esphome.io/projects/?type=bluetooth).

## Tuning

Blink timing lives in `KBeaconBlinkButton._ring_kwargs()` in `button.py`:

- `ring_ms` — total blink duration (ms)
- `led_on` / `led_off` — pulse cadence (smaller `led_off` = faster strobe)

## Protocol

The tag exposes the KBeacon config GATT service (`FEA0`), with write characteristic
`FEA1`, notify `FEA2`, and indicate `FEA3`. Authentication is an MD5
challenge–response using the access password.

**Ring** is a chunked JSON ADU on `FEA1`:
`{"msg":"ring","ringTime":<ms>,"ringType":<0|1|2>,"ledOn":<ms>,"ledOff":<ms>}`.

**Button events** are configured by writing a trigger with action `Report2App`
(`0x10`) for each gesture, then subscribing to **indications** on `FEA3`:
`{"msg":"cfg","trObj":[{"trIdx":0,"trType":3,"trAct":16}, ...]}`
(`trType`: 3=hold, 4=single, 5=double, 6=triple). Each `FEA3` indication carries
the gesture in `data[0] & 0x3F`. This follows the current vendor SDK
([`kkmcn/android_kbeaconlib2`](https://github.com/kkmcn/android_kbeaconlib2)).

## Disclaimer

Not affiliated with Blue Charm Beacons or the KBeacon project. Reverse-engineered
from the public SDK for personal use. Provided as-is.
