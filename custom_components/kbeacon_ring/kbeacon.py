"""KBeacon (Blue Charm) BLE protocol: MD5 auth + ring command + config read.

Reverse-engineered from kbeaconlib-release-v1.05.aar. Auth is MD5 challenge-
response over (mac_reversed + 0xA9 0xB1 + nonce + password). Commands/reads are
JSON framed over GATT.

App->device write frames: [0x20|pduTag][seq u16 be][utf-8 chunk]  (pduTag 0=first
1=mid 2=last 3=whole). Device->app data frames: [0x30|frameType][seq u16 be]
[utf-8 chunk]; app must ACK each with [0x33][recvLen u16][window=1000 u16]
[cause u16]. frameType 2/3 => JSON complete.

Auth notifications: [0x13][phase][payload]; strip 0x13, phase is byte[1].
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import struct

from .const import NOTIFY_UUID, WRITE_UUID

_LOGGER = logging.getLogger(__name__)

FACTOR = bytes([0xA9, 0xB1])
AUTH_FAIL_TAG = 0xF1
CMD_MARKER = 0x13


def _mac_reversed(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError("bad MAC: %s" % mac)
    return raw[::-1]


class KBeaconSession:
    """Drive auth + ring + config-read over an already-connected BleakClient."""

    def __init__(self, client, mac: str, password: str) -> None:
        self._client = client
        self._mac_rev = _mac_reversed(mac)
        self._pw = password.encode("ascii")
        self._app_random = b""
        self._mtu = 20
        self._auth_event = asyncio.Event()
        self._auth_failed = False
        self._cmd_done = asyncio.Event()
        self._json = None
        # config-read reassembly
        self._read_buf = ""
        self._read_done = asyncio.Event()
        self._read_result = None

    def _build_phase1(self) -> bytes:
        import random

        n = random.randint(0, 0x0FFFFFFF)
        self._app_random = struct.pack(">I", n)
        return bytes([CMD_MARKER, 0x01]) + self._app_random

    async def _on_notify(self, _char, data: bytearray) -> None:
        b = bytes(data)
        if not b:
            return
        _LOGGER.debug("kbeacon notify: %d bytes hex=%s", len(b), b.hex())
        # Auth-path notifications: [0x13][phase][payload...].
        if b[0] == CMD_MARKER and len(b) >= 2:
            phase = b[1]
            payload = b[2:]
            if phase in (0x01, 0x0B):
                await self._auth_phase1_response(payload, short=(phase == 0x0B))
            elif phase == 0x02:
                if payload and payload[0] > 3:
                    self._mtu = payload[0] - 3
                self._auth_failed = False
                self._auth_event.set()
            elif (phase & 0xFF) == AUTH_FAIL_TAG:
                self._auth_failed = True
                self._auth_event.set()
            return
        if b[0] == AUTH_FAIL_TAG:
            self._auth_failed = True
            self._auth_event.set()
            return
        # Device->app config DATA report frames: [0x30|frameType][seq u16][payload]
        if (b[0] & 0xF0) == 0x30 and len(b) >= 3:
            await self._handle_read_frame(b)
            return
        # Device->app ack of our config WRITE: [0x20|pduTag][seq u16] — asks next chunk
        if (b[0] & 0xF0) == 0x20 and len(b) >= 3:
            seq = struct.unpack(">H", b[1:3])[0]
            if self._json is not None and seq >= len(self._json):
                self._cmd_done.set()
            else:
                await self._send_chunk(seq)

    async def _auth_phase1_response(self, payload: bytes, short: bool) -> None:
        need = 12 if short else 20
        if len(payload) < need:
            _LOGGER.warning(
                "kbeacon auth: phase1 payload too short (%d < %d)", len(payload), need
            )
            self._auth_failed = True
            self._auth_event.set()
            return
        dev_random = payload[0:4]
        proof_len = 8 if short else 16
        dev_proof = payload[4 : 4 + proof_len]
        m = hashlib.md5(self._mac_rev + FACTOR + self._app_random + self._pw).digest()
        expect = bytes(m[i] ^ m[i + 8] for i in range(8)) if short else m
        if dev_proof != expect:
            _LOGGER.warning(
                "kbeacon auth: device proof mismatch (wrong password?) got=%s expect=%s",
                dev_proof.hex(),
                expect.hex(),
            )
            self._auth_failed = True
            self._auth_event.set()
            return
        m2 = hashlib.md5(self._mac_rev + FACTOR + dev_random + self._pw).digest()
        if short:
            payload2 = bytes(m2[i] ^ m2[i + 8] for i in range(8))
            frame = bytes([CMD_MARKER, 0x0C]) + payload2
        else:
            frame = bytes([CMD_MARKER, 0x02]) + m2
        _LOGGER.debug("kbeacon auth: phase1 verified; sending phase2 %s", frame.hex())
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)

    async def _send_chunk(self, seq: int) -> None:
        if self._json is None or seq >= len(self._json):
            self._cmd_done.set()
            return
        chunk_sz = self._mtu - 3
        total = len(self._json)
        if total <= chunk_sz:
            pdutag, dlen, seq = 3, total, 0
        elif seq == 0:
            pdutag, dlen = 0, chunk_sz
        elif seq + chunk_sz < total:
            pdutag, dlen = 1, chunk_sz
        else:
            pdutag, dlen = 2, total - seq
        head = bytes([0x20 | pdutag]) + struct.pack(">H", seq)
        body = self._json[seq : seq + dlen].encode("utf-8")
        _LOGGER.debug(
            "kbeacon cmd: write tag=%d seq=%d head+body=%s",
            pdutag,
            seq,
            (head + body).hex(),
        )
        await self._client.write_gatt_char(WRITE_UUID, head + body, response=True)

    async def _send_read_ack(self, recv_len: int, cause: int = 0) -> None:
        frame = (
            bytes([0x33])
            + struct.pack(">H", recv_len)
            + struct.pack(">H", 1000)
            + struct.pack(">H", cause)
        )
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)

    async def _handle_read_frame(self, b: bytes) -> None:
        frame_type = b[0] & 0x0F
        seq = struct.unpack(">H", b[1:3])[0]
        payload = b[3:].decode("utf-8", "replace")
        complete = False
        if frame_type == 0:
            self._read_buf = payload
            await self._send_read_ack(len(self._read_buf))
        elif frame_type in (1, 2):
            if seq != len(self._read_buf):
                await self._send_read_ack(len(self._read_buf), cause=1)
            else:
                self._read_buf += payload
                await self._send_read_ack(len(self._read_buf))
                if frame_type == 2:
                    complete = True
        elif frame_type == 3:
            self._read_buf = payload
            await self._send_read_ack(len(self._read_buf))
            complete = True
        if complete:
            try:
                self._read_result = _json.loads(self._read_buf)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("kbeacon read: bad json: %s (%s)", self._read_buf, exc)
                self._read_result = None
            self._read_done.set()

    async def authenticate(self, timeout: float = 15.0) -> bool:
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)
        _LOGGER.debug("kbeacon auth: notify subscribed; writing phase1")
        await self._client.write_gatt_char(WRITE_UUID, self._build_phase1(), response=True)
        _LOGGER.debug("kbeacon auth: phase1 written; waiting for response")
        try:
            await asyncio.wait_for(self._auth_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.error("kbeacon auth: timeout")
            return False
        return not self._auth_failed

    async def read_config(self, stype: int = 32, timeout: float = 10.0):
        """Read a config block (stype 32 = common; contains bCap capability)."""
        self._read_buf = ""
        self._read_result = None
        self._read_done.clear()
        req = {"msg": "getPara", "stype": int(stype)}
        self._json = _json.dumps(req, separators=(",", ":"))
        _LOGGER.debug("kbeacon read: requesting stype=%d json=%s", stype, self._json)
        await self._send_chunk(0)
        try:
            await asyncio.wait_for(self._read_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning("kbeacon read: timeout waiting for config stype=%d", stype)
            return None
        finally:
            self._json = None
        _LOGGER.info("kbeacon read: stype=%d result=%s", stype, self._read_result)
        return self._read_result

    async def ring(
        self,
        ring_ms: int = 5000,
        ring_type: int = 2,
        led_on: int = 200,
        led_off: int = 1800,
        timeout: float = 8.0,
    ) -> None:
        self._cmd_done.clear()
        cmd = {"msg": "ring", "ringTime": int(ring_ms), "ringType": int(ring_type)}
        if ring_type in (0, 2):
            cmd["ledOn"] = led_on
            cmd["ledOff"] = led_off
        self._json = _json.dumps(cmd, separators=(",", ":"))
        _LOGGER.debug("kbeacon ring: sending %s", self._json)
        await self._send_chunk(0)
        try:
            await asyncio.wait_for(self._cmd_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("kbeacon ring: no explicit completion ack (likely fine)")
        finally:
            self._json = None
        # The tag only drives the LED/buzzer WHILE the BLE central stays
        # connected. Disconnecting right after the ack truncates the effect to
        # a single flash/blip. Hold the link for the full ring window so the
        # device completes the whole ringTime before we let the caller drop
        # the connection.
        hold_s = max(1.0, ring_ms / 1000.0 + 0.5)
        _LOGGER.debug("kbeacon ring: holding connection %.1fs for ring window", hold_s)
        await asyncio.sleep(hold_s)
