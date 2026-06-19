"""``ensure_shared_connected`` must reconnect a shared session whose
transport has FATALLY died, instead of reusing the zombie.

Root cause of the prod "BrokenResourceError on tool refresh": a
service_account sandbox expired under a live session, the ClientSession
object stayed registered, and ``ensure_shared_connected`` early-returned
on ``shared_session is not None`` alone — handing the refresh a dead
session whose first send raised ``BrokenResourceError``. The fix gates
that early return on ``shared_task.is_transport_alive()``.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamDefinition,
    UpstreamSelfDescription,
)
from tests.unit.factories import make_upstream_definition


class _FakeTask:
    """Minimal ConnectionTask stand-in. ``is_transport_alive`` is the
    only behaviour the heal gate reads; ``close`` is awaited by the
    manager when it drops a session during reconnect."""

    def __init__(self, *, alive: bool) -> None:
        self._alive = alive
        self.server_info: ServerInfo | None = None
        self.self_description: UpstreamSelfDescription | None = None
        self.closed = False

    def is_transport_alive(self) -> bool:
        return self._alive

    async def close(self) -> None:
        self.closed = True


def _seed_live_shared(
    mgr: UpstreamClientManager, upstream: UpstreamDefinition, *, alive: bool,
) -> _FakeTask:
    """Put ``upstream`` into LIVE with a shared session backed by a
    fake task whose transport liveness the test controls."""
    task = _FakeTask(alive=alive)
    mgr.transition_to_live_shared(
        upstream.id,
        session=cast(Any, object()),
        task=cast(Any, task),
        server_info=ServerInfo(name="srv", version="1"),
        self_description=UpstreamSelfDescription(name="srv", version="1"),
    )
    return task


def _record_connect_shared(mgr: UpstreamClientManager) -> list[str]:
    """Replace ``connect_shared`` with a recorder so the heal path can
    be observed without a real MCP handshake."""
    calls: list[str] = []

    async def _record(
        up: UpstreamDefinition, *args: Any, **kwargs: Any,
    ) -> None:
        calls.append(up.id)

    mgr.connect_shared = _record  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_ensure_shared_connected_reuses_a_live_session() -> None:
    upstream = make_upstream_definition(id="everything2")
    mgr = UpstreamClientManager(upstreams=[upstream])
    _seed_live_shared(mgr, upstream, alive=True)
    calls = _record_connect_shared(mgr)

    await mgr.ensure_shared_connected(upstream)

    assert calls == [], "a healthy live session must be reused, not reconnected"


@pytest.mark.asyncio
async def test_ensure_shared_connected_reconnects_a_dead_session() -> None:
    upstream = make_upstream_definition(id="everything2")
    mgr = UpstreamClientManager(upstreams=[upstream])
    _seed_live_shared(mgr, upstream, alive=False)
    calls = _record_connect_shared(mgr)

    await mgr.ensure_shared_connected(upstream)

    assert calls == ["everything2"], (
        "a session whose transport has fatally died must be reconnected "
        "(connect_shared), not reused as a zombie"
    )


@pytest.mark.asyncio
async def test_ensure_and_reconnect_fresh_race_to_one_connect() -> None:
    """R1 cross-pool (review item 3): a heal holding the shared-connect lock
    mid-connect, racing a concurrent lazy ``ensure_shared_connected``, must
    yield exactly ONE ``connect_shared`` — the ensure blocks on the lock and
    then reuses the heal's fresh session via the double-check, instead of
    opening a second sandbox (two sandboxes, one orphaned, + a ref race)."""
    upstream = make_upstream_definition(id="everything2")
    mgr = UpstreamClientManager(upstreams=[upstream])

    calls = 0
    gate = asyncio.Event()

    async def gated_connect(up: UpstreamDefinition, *_a: Any, **_k: Any) -> None:
        nonlocal calls
        calls += 1
        await gate.wait()
        # Install a live shared session, as the real connect_shared would.
        mgr.transition_to_live_shared(
            up.id,
            session=cast(Any, object()),
            task=cast(Any, _FakeTask(alive=True)),
            server_info=ServerInfo(name="srv", version="1"),
            self_description=UpstreamSelfDescription(name="srv", version="1"),
        )

    mgr.connect_shared = gated_connect  # type: ignore[method-assign]

    # Heal: acquires the shared-connect lock, enters gated_connect, blocks on
    # the gate (still holding the lock).
    heal = asyncio.create_task(mgr.reconnect_shared_fresh(upstream))
    await asyncio.sleep(0.05)
    assert calls == 1

    # Concurrent lazy acquire arrives while the heal holds the lock.
    ensure = asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await asyncio.sleep(0.05)
    assert calls == 1, "the ensure must block on the lock, not connect in parallel"

    gate.set()  # heal finishes: installs the live session, releases the lock
    await asyncio.gather(heal, ensure)

    assert calls == 1, (
        "ensure must reuse the heal's fresh session via the double-check, "
        "not open a second sandbox"
    )
