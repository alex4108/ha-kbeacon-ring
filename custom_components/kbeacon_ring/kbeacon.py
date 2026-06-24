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

from .const import IND_UUID, NOTIFY_UUID, WRITE_UUID

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
        # Device->app 0x2X frames carry BOTH write-acks AND getPara responses:
        #   write-ack:     [0x20|pduTag][seq u16] (asks for next chunk / done)
        #   getPara reply: [0x23][len u16][0000][0000]{json...}  (frameType 3)
        # Distinguish by a trailing JSON body ('{' ... '}').
        if (b[0] & 0xF0) == 0x20 and len(b) >= 3:
            body_start = b.find(b"{")
            if body_start != -1 and b.rstrip(b"\x00").endswith(b"}"):
                # getPara JSON response (single complete frame).
                json_bytes = b[body_start:]
                try:
                    self._read_result = _json.loads(
                        json_bytes.decode("utf-8", "replace")
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "kbeacon read: bad getPara json: %s (%s)",
                        json_bytes, exc,
                    )
                    self._read_result = None
                self._read_done.set()
                return
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

    async def read_config(
        self, stype: int = 32, timeout: float = 10.0, tr_type: int | None = None
    ):
        """Read a config block (stype 32 = common; contains bCap capability).

        For the TRIGGER block (stype 64) the SDK's ``readTriggerConfig`` sends
        ``{"msg":"getPara","stype":64,"trType":<n>}`` — it MUST include
        ``trType`` (1=motion, 2=button), otherwise the device replies with a
        bare ``0x23 .. 0103`` ack and no data report (the timeout we kept
        hitting). Pass ``tr_type`` to read a specific trigger's config. The
        device returns the trigger objects under a ``trObj`` JSON array.
        """
        self._read_buf = ""
        self._read_result = None
        self._read_done.clear()
        req = {"msg": "getPara", "stype": int(stype)}
        if tr_type is not None:
            req["trType"] = int(tr_type)
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

    async def read_para(self, sub_type: int, extra: dict | None = None,
                        timeout: float = 10.0):
        """Read a config block using the CURRENT (kbeaconlib2) wire format.

        The modern SDK's getPara uses field ``type`` (KBCfgType): 1=CommonPara,
        2=AdvPara, 4=TriggerPara, 8=SensorPara — NOT the old ``stype`` field.
        Sending the wrong field/value is why earlier reads returned a bare
        ``0x23 .. 0103`` reject. CommonPara (type=1) carries ``btPt`` (battery
        percent), ``bCap`` and the device basics.

        Returns the parsed JSON dict, or None on timeout.
        """
        self._read_buf = ""
        self._read_result = None
        self._read_done.clear()
        req = {"msg": "getPara", "type": int(sub_type)}
        if extra:
            req.update(extra)
        self._json = _json.dumps(req, separators=(",", ":"))
        _LOGGER.debug("kbeacon read_para: requesting %s", self._json)
        await self._send_chunk(0)
        try:
            await asyncio.wait_for(self._read_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning("kbeacon read_para: timeout for type=%d", sub_type)
            return None
        finally:
            self._json = None
        _LOGGER.debug("kbeacon read_para: type=%d result=%s", sub_type, self._read_result)
        return self._read_result

    async def read_battery(self, timeout: float = 10.0):
        """Read battery percent (btPt) from CommonPara. None if unavailable."""
        common = await self.read_para(1, timeout=timeout)  # 1 = CommonPara
        if not common:
            return None
        bt = common.get("btPt")
        try:
            pct = int(bt) if bt is not None else None
        except (TypeError, ValueError):
            return None
        if pct is None:
            return None
        # Fresh CR2477 cells read slightly over 100 on the device's scale;
        # clamp to a sane 0..100 for HA.
        return max(0, min(100, pct))

    async def write_config(self, params: dict, timeout: float = 8.0) -> None:
        """Write a config block to the tag.

        Per the decompiled SDK (KBCfgHandler.objectsToParaDict), a config
        change is a single JSON object ``{"msg":"cfg","stype":<bitmask>,...}``
        carrying the changed fields, sent over the SAME chunked ADU write path
        the ring command proves out. ``stype`` is the OR of each touched cfg
        block's ``cfgParaType`` (trigger block = 64).

        ``params`` should already contain the cfg fields (e.g. trType/trAct/
        trPara/trAType/trATm) and the correct ``stype``; ``msg`` is added here.
        """
        self._cmd_done.clear()
        body = {"msg": "cfg"}
        body.update(params)
        self._json = _json.dumps(body, separators=(",", ":"))
        _LOGGER.info("kbeacon cfg: writing %s", self._json)
        await self._send_chunk(0)
        try:
            await asyncio.wait_for(self._cmd_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("kbeacon cfg: no explicit completion ack (likely fine)")
        finally:
            self._json = None

    async def write_trigger_report2app(
        self, gestures: list[int], timeout: float = 8.0
    ) -> None:
        """Configure button gestures to report live events to the app (FEA3).

        Modern kbeaconlib2 wire format: triggers go in a ``trObj`` ARRAY, each
        ``{"trIdx":i,"trType":<gesture>,"trAct":16}`` where 16 = Report2App.
        gesture KBTriggerType: 3=long 4=single 5=double 6=triple. This is what
        makes a press emit an INDICATION on FEA3 (vs Advertisement=1 which only
        broadcasts). Sent over the same chunked FEA1 path; device acks on FEA2.
        """
        tr_obj = [
            {"trIdx": i, "trType": int(g), "trAct": 0x10}
            for i, g in enumerate(gestures)
        ]
        self._cmd_done.clear()
        body = {"msg": "cfg", "trObj": tr_obj}
        self._json = _json.dumps(body, separators=(",", ":"))
        _LOGGER.info("kbeacon trigger: writing %s", self._json)
        await self._send_chunk(0)
        try:
            await asyncio.wait_for(self._cmd_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("kbeacon trigger: no explicit completion ack (likely fine)")
        finally:
            self._json = None

    async def subscribe_button_events(self, on_gesture) -> None:
        """Start INDICATIONS on FEA3 and decode button gestures.

        Each indication: ``data[0] & 0x3F`` == KBTriggerType gesture; the rest
        is the event body. ``on_gesture(gesture_int, body_bytes)`` is invoked.
        bleak's start_notify handles the indication CCCD automatically.
        """
        self._on_gesture = on_gesture

        def _ind_cb(_char, data: bytearray) -> None:
            if not data:
                return
            gesture = data[0] & 0x3F
            body = bytes(data[1:])
            _LOGGER.info(
                "kbeacon FEA3 indication: gesture=%d raw=%s",
                gesture,
                bytes(data).hex(),
            )
            try:
                on_gesture(gesture, body)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("kbeacon FEA3: gesture handler error: %s", exc)

        await self._client.start_notify(IND_UUID, _ind_cb)
        _LOGGER.info("kbeacon FEA3: subscribed to button indications")

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
