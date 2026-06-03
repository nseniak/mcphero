"""Test seed helpers that drive the state-machine API.

Tests previously poked the manager's private dicts (``_sessions``,
``_admin_sessions``, ``_self_descriptions``, ...) to set up the
state they wanted to assert on. Those dicts have been folded into
``UpstreamState``; the seeds here go through the public
``transition_to_*`` surface so tests stay one step removed from the
storage shape and survive the next refactor.

Per-user sessions stay in their own dicts on the manager (they're
orthogonal to upstream-level state) — the per-user seed helper
populates those directly because there's no public mutation surface
for them today.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from mcpolis.adapters.upstream_clients.client_manager import (
    ADMIN_USER_ID,
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)


def stub_client_session(name: str = "ClientSession") -> Any:
    """Return a sentinel ``ClientSession``-shaped mock."""
    return MagicMock(name=name)


def stub_connection_task() -> MagicMock:
    """Return a sentinel ``ConnectionTask`` whose ``close()`` is an
    awaitable mock — letting tests assert teardown awaited it."""
    task = MagicMock(name="ConnectionTask")
    task.close = AsyncMock()
    task.server_info = None
    task.self_description = None
    return task


def seed_shared_session(
    mgr: UpstreamClientManager,
    upstream_id: str,
    *,
    session: Any = None,
    task: MagicMock | None = None,
    server_info: ServerInfo | None = None,
    self_description: UpstreamSelfDescription | None = None,
) -> tuple[Any, MagicMock]:
    """Seed a live shared session via ``transition_to_live_shared``.

    Returns ``(session, task)`` so tests can assert on ``task.close``
    awaits and identity-compare the session.
    """
    if session is None:
        session = stub_client_session()
    if task is None:
        task = stub_connection_task()
    mgr.transition_to_live_shared(
        upstream_id,
        session=session,
        task=task,
        server_info=server_info,
        self_description=self_description,
    )
    return session, task


def seed_admin_session(
    mgr: UpstreamClientManager,
    upstream_id: str,
    *,
    session: Any = None,
    task: MagicMock | None = None,
    server_info: ServerInfo | None = None,
    self_description: UpstreamSelfDescription | None = None,
) -> tuple[Any, MagicMock]:
    """Seed a live admin OAuth session via ``transition_to_live_admin``."""
    if session is None:
        session = stub_client_session()
    if task is None:
        task = stub_connection_task()
    mgr.transition_to_live_admin(
        upstream_id,
        session=session,
        task=task,
        server_info=server_info,
        self_description=self_description,
    )
    return session, task


def seed_user_session(
    mgr: UpstreamClientManager,
    upstream_id: str,
    user_id: str,
    *,
    session: Any = None,
    task: MagicMock | None = None,
) -> tuple[Any, MagicMock]:
    """Seed a per-user session.

    For ``ADMIN_USER_ID`` routes to the admin slot on the upstream
    state record (matching production's
    ``connect_upstream_for_user(ADMIN_USER_ID)`` shape). For real
    users populates the orthogonal per-user dicts directly.
    """
    if session is None:
        session = stub_client_session()
    if task is None:
        task = stub_connection_task()
    if user_id == ADMIN_USER_ID:
        return seed_admin_session(
            mgr, upstream_id, session=session, task=task,
        )
    key = (user_id, upstream_id)
    mgr._user_sessions[key] = session  # pyright: ignore[reportPrivateUsage]
    mgr._user_tasks[key] = task  # pyright: ignore[reportPrivateUsage]
    mgr._user_session_last_used[key] = 0.0  # pyright: ignore[reportPrivateUsage]
    return session, task


def seed_self_description(
    mgr: UpstreamClientManager,
    upstream_id: str,
    self_description: UpstreamSelfDescription,
) -> None:
    """Set the cached ``self_description`` on the state record without
    opening a session.

    Used by tests that only care about the metadata accessor — e.g.,
    gateway-instructions tests that exercise the per-upstream
    description rendering. Mirrors what ``connect_shared`` would do
    at the metadata layer, minus the live transport.
    """
    state = mgr.get_state(upstream_id)
    if state is None:
        # The upstream wasn't registered with the manager. Tests
        # constructing ``UpstreamClientManager(upstreams=[upstream])``
        # avoid this; the raise here flags malformed setups.
        raise KeyError(
            f"upstream {upstream_id!r} not registered on manager"
        )
    state.self_description = self_description
