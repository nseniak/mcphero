"""Shared :class:`ExitSignal` implementation for sandbox backends.

Both the E2B and local-subprocess sandbox services need the same
mechanic: a per-session exit-event + bounded stderr tail buffer + an
exit-code slot. This module centralises that so the
``SubprocessExitedDuringInit`` fast-fail path in the connection task
sees identical behaviour regardless of which backend is in use.
"""
from __future__ import annotations

import asyncio

from mcpolis.domain.services.sandbox_service import (
    STDERR_TAIL_BYTES,
    ProcessExitSnapshot,
)


class ExitSignalImpl:
    """Concrete :class:`ExitSignal` for sandbox-service implementations.

    Thread-safety: all mutators are designed to run on the asyncio
    event loop only. ``append_stderr`` is called from stream-callback
    tasks; ``mark_exited`` from the per-process watcher task; ``wait``
    / ``snapshot`` from the connection task. No locks needed under
    asyncio's single-threaded execution model.
    """

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()
        self._exit_code: int | None = None
        self._stderr_tail: bytearray = bytearray()

    def append_stderr(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._stderr_tail.extend(chunk)
        overflow = len(self._stderr_tail) - STDERR_TAIL_BYTES
        if overflow > 0:
            del self._stderr_tail[:overflow]

    def mark_exited(self, exit_code: int | None) -> None:
        # Only the FIRST exit observation wins. The E2B watcher task
        # gets re-spawned after a sandbox auto-pause/resume cycle (the
        # streaming RPC drops, ``wait()`` returns, even though the
        # process inside the sandbox is still alive). Treating the
        # first observation as authoritative means the snapshot the
        # connection task reads always reflects the *real* exit if one
        # happened during the init window.
        if self._event.is_set():
            return
        self._exit_code = exit_code
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def snapshot(self) -> ProcessExitSnapshot:
        return ProcessExitSnapshot(
            exit_code=self._exit_code,
            stderr_tail=bytes(self._stderr_tail).decode(
                "utf-8", errors="replace",
            ),
        )


__all__ = ["ExitSignalImpl"]
