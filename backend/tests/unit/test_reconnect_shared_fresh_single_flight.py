"""``reconnect_shared_fresh`` single-flight: concurrent stall-heals must
COALESCE onto one fresh reconnect (R1, BLOCKER).

The dispatch stall-recovery's new ping-gated detection clusters concurrent
stalls on the SAME poisoned shared session into a near-simultaneous burst
of ``heal_stalled_session`` → ``reconnect_shared_fresh``. That path
bypasses ``ensure_shared_connected``'s ``_lazy_connect_tasks``
single-flight (it deletes the persisted sandbox ref and calls
``connect_shared`` directly), so without its own single-flight, N
simultaneous healers would force N fresh E2B sandboxes — N-1 immediately
orphaned — and race the ref delete/re-persist.

These are real-asyncio concurrency tests (racing tasks), not code-reads —
the spec requires it; the real-E2B integration test pins the same
invariant against a live sandbox.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from tests.unit.factories import make_upstream_definition


@pytest.mark.asyncio
async def test_concurrent_healers_coalesce_to_one_reconnect() -> None:
    upstream = make_upstream_definition(id="everything2")  # service_account
    mgr = UpstreamClientManager(upstreams=[upstream])

    started = 0
    release = asyncio.Event()

    async def slow_connect(up: Any, *a: Any, **k: Any) -> None:
        nonlocal started
        started += 1
        # Hold the first reconnect in-flight while the siblings pile up on
        # the single-flight gate, so a missing single-flight would show as
        # started > 1.
        await release.wait()

    mgr.connect_shared = slow_connect  # type: ignore[method-assign]

    healers = [
        asyncio.create_task(mgr.reconnect_shared_fresh(upstream))
        for _ in range(8)
    ]
    await asyncio.sleep(0.05)  # let all 8 reach the gate
    assert started == 1, "only the first healer may start a reconnect"
    release.set()
    await asyncio.gather(*healers)

    assert started == 1, (
        "8 concurrent healers must coalesce onto ONE connect_shared, "
        "not create 8 sandboxes (7 orphaned)"
    )


@pytest.mark.asyncio
async def test_sequential_healers_each_reconnect() -> None:
    """The single-flight must coalesce only TRULY concurrent healers — a
    heal that arrives after a prior reconnect already completed reflects a
    genuinely later stall and must force its own fresh reconnect."""
    upstream = make_upstream_definition(id="everything2")
    mgr = UpstreamClientManager(upstreams=[upstream])

    started = 0

    async def fast_connect(up: Any, *a: Any, **k: Any) -> None:
        nonlocal started
        started += 1

    mgr.connect_shared = fast_connect  # type: ignore[method-assign]

    await mgr.reconnect_shared_fresh(upstream)
    await mgr.reconnect_shared_fresh(upstream)

    assert started == 2, "sequential heals each force a fresh reconnect"
