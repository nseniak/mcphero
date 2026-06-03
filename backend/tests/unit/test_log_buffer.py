"""LogBuffer ring + subscribe() invariants."""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer


@pytest.mark.asyncio
async def test_subscribe_yields_writes_after_ring_overflow() -> None:
    """The streaming bug: when the ring evicts old chunks, the
    string-length cursor used to slice the wrong window and either
    drop new content or yield empty. The fixed cursor is an absolute
    byte offset, so post-eviction writes still stream correctly.

    Sized so each ``write()`` adds ~29 bytes (a timestamped
    one-line entry) and a max_bytes that holds two such entries —
    every third write evicts the oldest, exercising the ring path.
    """
    buf = LogBuffer(max_bytes=70)
    buf.bind_loop(asyncio.get_running_loop())

    # Initial write the late subscriber will see in the replay.
    buf.write("AAAAA")
    initial = buf.get_output()
    assert "AAAAA" in initial

    received: list[str] = []
    async def consume() -> None:
        async for chunk in buf.subscribe():
            received.append(chunk)
            if all(
                letter in "".join(received)
                for letter in ("BBBBB", "CCCCC", "DDDDD")
            ):
                return

    task = asyncio.create_task(consume())
    # Give the subscriber a tick to register and yield the initial.
    await asyncio.sleep(0.01)

    # Each subsequent write fits two-deep, then evicts.
    buf.write("BBBBB")
    await asyncio.sleep(0.01)
    buf.write("CCCCC")  # evicts AAAAA
    await asyncio.sleep(0.01)
    buf.write("DDDDD")  # evicts BBBBB
    await asyncio.sleep(0.01)

    await asyncio.wait_for(task, timeout=2)

    full = "".join(received)
    # Pre-fix bug: after CCCCC's eviction the cursor pointed past the
    # end of the newly-shorter buffer, so DDDDD silently dropped.
    # Post-fix: each write's content reaches the subscriber.
    assert "BBBBB" in full
    assert "CCCCC" in full
    assert "DDDDD" in full


@pytest.mark.asyncio
async def test_clear_does_not_break_live_subscriber() -> None:
    """clear() empties the live ring but keeps the absolute byte
    counter, so subscribers' cursors stay valid and they receive
    post-clear writes as a normal append.
    """
    buf = LogBuffer(max_bytes=1024)
    buf.bind_loop(asyncio.get_running_loop())
    buf.write("before-clear")

    received: list[str] = []
    async def consume() -> None:
        async for chunk in buf.subscribe():
            received.append(chunk)
            if any("after-clear" in c for c in received):
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    buf.clear()
    await asyncio.sleep(0.01)
    buf.write("after-clear")

    await asyncio.wait_for(task, timeout=2)

    full = "".join(received)
    assert "after-clear" in full


@pytest.mark.asyncio
async def test_subscribe_initial_yield_includes_existing_buffer() -> None:
    """A late subscriber sees what's already in the buffer first."""
    buf = LogBuffer(max_bytes=1024)
    buf.bind_loop(asyncio.get_running_loop())
    buf.write("hello")
    buf.write("world")

    chunks: list[str] = []
    async def consume() -> None:
        async for chunk in buf.subscribe():
            chunks.append(chunk)
            return  # Only need the first yield to test "initial replay".

    await asyncio.wait_for(asyncio.create_task(consume()), timeout=1)
    assert "hello" in chunks[0]
    assert "world" in chunks[0]
