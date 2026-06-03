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
