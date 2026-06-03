"""Ring-buffer TextIO for capturing subprocess stderr output.

Keeps the last ``max_bytes`` of output, discarding older data.
Thread-safe for concurrent writes (the subprocess stderr reader)
and reads (the API endpoint / SSE stream).
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from io import TextIOBase

DEFAULT_MAX_BYTES = 64 * 1024  # 64 KB


class LogBuffer(TextIOBase):
    """A TextIO that keeps the last max_bytes of written data.

    Supports async waiters for new data via :meth:`wait_for_new_data`.

    SSE-friendly streaming via :meth:`subscribe`. Subscribers track an
    *absolute* byte offset (``_total_written``, monotonically
    increasing across the buffer's whole lifetime) rather than the
    *current* string length, so eviction of older content doesn't
    desynchronise the cursor — when the ring overflows, the
    "subscriber's offset" still points correctly at the next-to-be-
    sent byte; ring eviction just means the subscriber may miss bytes
    that fell off the back, which is the documented ring semantics
    anyway. The previous string-length cursor was wrong: after
    eviction it sliced the wrong window of the buffer and silently
    dropped new content (or yielded duplicates), so streaming
    "stopped working" once the 64 KB cap was reached.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._chunks: deque[str] = deque()
        self._total_bytes = 0
        # Monotonic offset of the *next* byte that will be appended.
        # The cumulative sum of every byte ever written; never
        # decreases, even when chunks are evicted.
        self._total_written = 0
        # Offset of the first byte still in the live ring. Advances
        # past evicted prefix bytes so subscribers can compute
        # ``cursor - _ring_start`` to slice correctly.
        self._ring_start = 0
        self._version = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: list[asyncio.Event] = []

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the event loop so write() can notify async waiters."""
        self._loop = loop

    def write(self, s: str) -> int:
        if not s:
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        stamped = "".join(
            f"[{now}] {line}\n"
            for line in s.splitlines()
        )
        with self._lock:
            self._chunks.append(stamped)
            self._total_bytes += len(stamped)
            self._total_written += len(stamped)
            self._version += 1
            self._trim()
        # Notify async waiters from the event loop thread
        if self._loop is not None and self._waiters:
            self._loop.call_soon_threadsafe(self._notify_waiters)
        return len(s)

    def _notify_waiters(self) -> None:
        for event in self._waiters:
            event.set()

    def _trim(self) -> None:
        """Discard oldest chunks until total size is within the limit."""
        while self._total_bytes > self._max_bytes and self._chunks:
            removed = self._chunks.popleft()
            self._total_bytes -= len(removed)
            self._ring_start += len(removed)

    @property
    def version(self) -> int:
        """Monotonically increasing counter, bumped on each write."""
        return self._version

    def get_output(self) -> str:
        """Return the buffered output as a single string."""
        with self._lock:
            return "".join(self._chunks)

    async def wait_for_new_data(self, timeout: float = 5.0) -> bool:
        """Wait until new data is written or timeout expires.

        Returns True if new data arrived, False on timeout.
        """
        event = asyncio.Event()
        self._waiters.append(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            self._waiters.remove(event)

    async def subscribe(self) -> AsyncIterator[str]:
        """Yield the current contents, then yield new chunks as they arrive.

        This is an async generator intended for SSE streaming.

        The subscriber tracks ``cursor`` as an absolute byte offset
        into the lifetime stream, not a string position into the
        live ring — so eviction of old content past the back of the
        ring doesn't cause the next yield to slice the wrong window.
        """
        with self._lock:
            current = "".join(self._chunks)
            cursor = self._total_written
        if current:
            yield current

        while True:
            arrived = await self.wait_for_new_data(timeout=15)
            if not arrived:
                yield ""  # keepalive signal
                continue
            with self._lock:
                full = "".join(self._chunks)
                new_total = self._total_written
                new_ring_start = self._ring_start
            if new_total <= cursor:
                # No bytes appended since last yield — nothing to do.
                # (Shouldn't normally happen because wait_for_new_data
                # only fires on writes, but be defensive.)
                continue
            # Slice the bytes after ``cursor``. Any bytes before
            # ``new_ring_start`` were evicted while the subscriber
            # wasn't looking — those are unrecoverable, just clamp.
            slice_from = max(cursor, new_ring_start)
            new_part = full[slice_from - new_ring_start:]
            if new_part:
                yield new_part
            cursor = new_total

    def clear(self) -> None:
        """Empty the live ring without resetting the lifetime offset.

        ``_total_written`` is the lifetime byte counter — never
        decreases — so any live subscriber's cursor stays valid
        across a clear(). We just empty the live chunks and advance
        ``_ring_start`` to the current cumulative offset, which is
        equivalent to "evicting everything currently in the ring."
        Next write picks up from the same lifetime offset; the
        subscriber sees the new content as a normal append.
        """
        with self._lock:
            self._chunks.clear()
            self._total_bytes = 0
            self._ring_start = self._total_written
            self._version = 0

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False
