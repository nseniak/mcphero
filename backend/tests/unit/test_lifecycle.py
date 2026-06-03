"""Tests for graceful drain lifecycle (Phase 4, Step 4)."""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.entrypoints.lifecycle import DrainCoordinator


@pytest.mark.asyncio
async def test_drain_completes_immediately_when_idle() -> None:
    """Drain with no active requests completes immediately."""
    dc = DrainCoordinator(drain_timeout=5.0)
    assert dc.is_draining is False
    await dc.drain()
    assert dc.is_draining is True


@pytest.mark.asyncio
async def test_drain_waits_for_active_requests() -> None:
    """Drain waits until all in-flight requests finish."""
    dc = DrainCoordinator(drain_timeout=5.0)
    dc.request_started()
    dc.request_started()
    assert dc.active_requests == 2

    # Start drain in background
    drain_done = False

    async def do_drain() -> None:
        nonlocal drain_done
        await dc.drain()
        drain_done = True

    task = asyncio.create_task(do_drain())
    await asyncio.sleep(0.05)
    assert not drain_done  # Still waiting

    dc.request_finished()
    await asyncio.sleep(0.05)
    assert not drain_done  # 1 request still active

    dc.request_finished()
    await asyncio.sleep(0.05)
    assert drain_done  # All done
    task.cancel()


@pytest.mark.asyncio
async def test_drain_times_out() -> None:
    """Drain times out if requests don't finish."""
    dc = DrainCoordinator(drain_timeout=0.1)
    dc.request_started()

    await dc.drain()  # Should return after timeout
    assert dc.is_draining is True
    assert dc.active_requests == 1  # Request still "active"


@pytest.mark.asyncio
async def test_request_tracking() -> None:
    """request_started / request_finished track counts correctly."""
    dc = DrainCoordinator()
    assert dc.active_requests == 0

    dc.request_started()
    assert dc.active_requests == 1

    dc.request_started()
    assert dc.active_requests == 2

    dc.request_finished()
    assert dc.active_requests == 1

    dc.request_finished()
    assert dc.active_requests == 0

    # Extra finish doesn't go negative
    dc.request_finished()
    assert dc.active_requests == 0


@pytest.mark.asyncio
async def test_healthz_endpoint_not_draining() -> None:
    """Verify DrainCoordinator starts not draining."""
    dc = DrainCoordinator()
    assert dc.is_draining is False


@pytest.mark.asyncio
async def test_healthz_endpoint_draining() -> None:
    """After drain, is_draining is True."""
    dc = DrainCoordinator(drain_timeout=0.1)
    await dc.drain()
    assert dc.is_draining is True
