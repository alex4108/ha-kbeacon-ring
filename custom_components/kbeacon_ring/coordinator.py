"""Connection-slot coordinator for a single KBeacon tag.

A KBeacon allows only ONE active BLE connection at a time, and the ESPHome
proxy exposes a single connectable slot. The button-event platform holds a
persistent connection (for FEA3 indications), which means the ring/chirp/blink
commands can't get a connection — they fail with "no connectable BLE route".

This coordinator lets a short-lived command (ring) YIELD the slot from the
long-lived listener:

    async with coordinator.command_slot():
        # listener has dropped its connection; the slot is free
        ... connect, auth, ring, disconnect ...
    # listener automatically reconnects after the block

The event platform registers a ``yield_handler`` (an async callable that drops
its connection) and a ``resume`` callback (sync, schedules reconnect). If no
listener is registered (e.g. button-events disabled), the context manager is a
no-op and the command just connects normally.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

_LOGGER = logging.getLogger(__name__)


class KBeaconCoordinator:
    """Mediates the single BLE connection slot between listener and commands."""

    def __init__(self) -> None:
        # Serialize commands so two ring presses don't race the slot.
        self._lock = asyncio.Lock()
        self._yield_handler = None  # async callable: drop listener connection
        self._resume_handler = None  # callable: tell listener to reconnect
        self._paused = False

    def register_listener(self, yield_handler, resume_handler) -> None:
        """Called by the event platform when it starts holding a connection."""
        self._yield_handler = yield_handler
        self._resume_handler = resume_handler

    def unregister_listener(self) -> None:
        self._yield_handler = None
        self._resume_handler = None

    @property
    def is_paused(self) -> bool:
        """True while the listener has yielded its slot for a command."""
        return self._paused

    @asynccontextmanager
    async def command_slot(self, settle: float = 1.5):
        """Acquire the BLE slot for a short command, yielding it from the listener.

        Serializes against other commands. If a listener is registered, asks it
        to drop its connection, waits briefly for the slot to free, runs the
        command body, then resumes the listener.
        """
        async with self._lock:
            yielded = False
            try:
                if self._yield_handler is not None:
                    _LOGGER.info("kbeacon coordinator: yielding slot for command")
                    self._paused = True
                    try:
                        await self._yield_handler()
                        yielded = True
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "kbeacon coordinator: yield_handler failed: %s", exc
                        )
                    # Give the stack a moment to actually release the slot.
                    await asyncio.sleep(settle)
                yield
            finally:
                if yielded and self._resume_handler is not None:
                    _LOGGER.info("kbeacon coordinator: resuming listener")
                    try:
                        self._resume_handler()
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "kbeacon coordinator: resume_handler failed: %s", exc
                        )
                self._paused = False
