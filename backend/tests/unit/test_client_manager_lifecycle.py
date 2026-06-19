"""Connection lifecycle tests for ``UpstreamClientManager``.

Pins the state-machine behavior of the shared-session vs per-user-session
bookkeeping: ``is_connected``, ``disconnect_upstream``, and the
``disconnect_user_session`` / ``disconnect_all_user_sessions`` helpers.
These methods drive the UI's "is this MCP reachable?" gate (via
``UpstreamConfigService.connection_status``) and the tool router's
session reuse logic — if a key-tuple bug ever leaks one user's MCP
session to another, or ``disconnect_upstream`` leaves an ``__admin__``
session dangling, everything downstream lies about connection state.

Tests drive the public state-machine surface (``transition_to_*``)
to seed sessions; assertions read through the typed accessors
(``is_connected``, ``has_admin_session``, etc.) and ``get_state``.
The behavior under test is the bookkeeping around the state record;
real session startup is exercised by ``test_mcp_integration``.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    ADMIN_USER_ID,
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
)
from mcpolis.domain.model.upstream import TransportType


def _make_manager() -> UpstreamClientManager:
    return UpstreamClientManager(upstreams=[])


def _stub_session() -> Any:
    """Sentinel stand-in for an ``mcp.client.session.ClientSession``.

    The lifecycle methods only look up sessions by key; they never call
    methods on them, so any identity-comparable value works.
    """
    return MagicMock(name="ClientSession")


def _stub_task() -> MagicMock:
    """Stub ``ConnectionTask`` whose ``close()`` is an AsyncMock, so we
    can assert it was awaited during teardown."""
    task = MagicMock(name="ConnectionTask")
    task.close = AsyncMock()
    task.server_info = None
    task.self_description = None
    return task


def _seed_shared(
    mgr: UpstreamClientManager, upstream_id: str
) -> MagicMock:
    """Seed a live shared session via the state-machine API.

    Equivalent to a successful ``connect_shared`` for the upstream,
    minus the actual transport/sandbox plumbing.
    """
    session = _stub_session()
    task = _stub_task()
    mgr.transition_to_live_shared(
        upstream_id,
        session=session,
        task=task,
        server_info=None,
        self_description=None,
    )
    return task


def _seed_user(
    mgr: UpstreamClientManager, upstream_id: str, user_id: str
) -> MagicMock:
    """Seed a per-user session (admin or real user).

    For ``ADMIN_USER_ID`` this advances the upstream into LIVE with
    the admin slot populated — matching what
    ``connect_admin_session`` would have produced. For real users
    the per-user dicts are populated directly (orthogonal to the
    upstream-level state machine).
    """
    session = _stub_session()
    task = _stub_task()
    if user_id == ADMIN_USER_ID:
        mgr.transition_to_live_admin(
            upstream_id,
            session=session,
            task=task,
            server_info=None,
            self_description=None,
        )
        return task
    key = (user_id, upstream_id)
    mgr._user_sessions[key] = session  # pyright: ignore[reportPrivateUsage]
    mgr._user_tasks[key] = task  # pyright: ignore[reportPrivateUsage]
    mgr._user_session_last_used[key] = 0.0  # pyright: ignore[reportPrivateUsage]
    return task


# ── is_connected: the "is this MCP reachable?" gate ──────────────────
#
# ``is_connected`` is the sole input to ``UpstreamConfigService``'s
# ``connection_status`` dict, which the ``/api/user/mcps`` endpoint
# uses to decide whether to show "unavailable" in the UI. A regression
# here silently hides per-user OAuth state behind a stale gate.


def test_is_connected_false_when_no_sessions() -> None:
    mgr = _make_manager()
    assert mgr.is_connected("notion") is False


def test_is_connected_true_for_shared_session_only() -> None:
    mgr = _make_manager()
    _seed_shared(mgr, "slack")
    assert mgr.is_connected("slack") is True


def test_is_connected_true_for_admin_user_session_only() -> None:
    """Key coupling: an ``__admin__`` per-user session (the shape
    ``admin_mcp_controller`` creates for OAuth upstreams) flips
    ``is_connected`` to True even without a shared session."""
    mgr = _make_manager()
    _seed_user(mgr, "notion", ADMIN_USER_ID)
    assert mgr.is_connected("notion") is True


def test_is_connected_false_when_only_non_admin_user_has_session() -> None:
    """A regular user's per-user session must NOT flip the upstream-
    level ``is_connected`` flag — otherwise the UI would show "ready"
    to every other user just because one of them authenticated."""
    mgr = _make_manager()
    _seed_user(mgr, "notion", "alice@example.com")
    assert mgr.is_connected("notion") is False


def test_is_connected_true_when_shared_and_admin_both_present() -> None:
    mgr = _make_manager()
    _seed_shared(mgr, "github")
    _seed_user(mgr, "github", ADMIN_USER_ID)
    assert mgr.is_connected("github") is True


# ── disconnect_upstream: clean teardown ──────────────────────────────
#
# ``disconnect_upstream`` is the load-bearing method for admin-initiated
# disconnects. It must close the shared session, tear down the
# ``__admin__`` per-user session, AND leave other users' sessions alone.
# If any of those invariants slip, you either leak sessions across
# restarts or wipe an unrelated user's connection.


@pytest.mark.asyncio
async def test_disconnect_upstream_closes_shared_session() -> None:
    mgr = _make_manager()
    task = _seed_shared(mgr, "slack")
    await mgr.disconnect_upstream("slack")

    assert mgr.is_connected("slack") is False
    state = mgr.get_state("slack")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    assert state.shared_session is None
    task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_upstream_closes_admin_user_session() -> None:
    mgr = _make_manager()
    admin_task = _seed_user(mgr, "notion", ADMIN_USER_ID)
    await mgr.disconnect_upstream("notion")

    assert mgr.is_connected("notion") is False
    assert mgr.has_user_session("notion", ADMIN_USER_ID) is False
    admin_task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_upstream_closes_both_shared_and_admin() -> None:
    """Both paths must fire on a single ``disconnect_upstream`` call —
    half-closed state is exactly how stale ``__admin__`` sessions end
    up flipping ``is_connected`` to True after the shared transport
    has already torn down."""
    mgr = _make_manager()
    shared_task = _seed_shared(mgr, "github")
    admin_task = _seed_user(mgr, "github", ADMIN_USER_ID)

    await mgr.disconnect_upstream("github")

    assert mgr.is_connected("github") is False
    shared_task.close.assert_awaited_once()
    admin_task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_upstream_preserves_other_users_sessions() -> None:
    """Admin disconnect must not touch per-user OAuth sessions for
    other users — they hold independent tokens and their sessions are
    the entire contract behind ``per_user_oauth``."""
    mgr = _make_manager()
    _seed_shared(mgr, "notion")
    alice_task = _seed_user(mgr, "notion", "alice@example.com")
    bob_task = _seed_user(mgr, "notion", "bob@example.com")

    await mgr.disconnect_upstream("notion")

    assert mgr.has_user_session("notion", "alice@example.com") is True
    assert mgr.has_user_session("notion", "bob@example.com") is True
    alice_task.close.assert_not_awaited()
    bob_task.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_upstream_does_not_touch_other_upstreams() -> None:
    """Disconnecting one upstream must leave every other upstream
    (shared or per-user) untouched."""
    mgr = _make_manager()
    notion_task = _seed_shared(mgr, "notion")
    slack_task = _seed_shared(mgr, "slack")
    slack_admin_task = _seed_user(mgr, "slack", ADMIN_USER_ID)

    await mgr.disconnect_upstream("notion")

    notion_task.close.assert_awaited_once()
    slack_task.close.assert_not_awaited()
    slack_admin_task.close.assert_not_awaited()
    assert mgr.is_connected("slack") is True


# ── disconnect_user_session / disconnect_all_user_sessions ───────────
#
# These drive per-user teardown (e.g. a user signing out, or an admin
# revoking a user's tokens). Both must be strict about only touching
# the targeted keys.


@pytest.mark.asyncio
async def test_disconnect_user_session_is_exact_tuple_match() -> None:
    """Must close exactly ``(user_id, upstream_id)`` — no other tuples.

    A regression where this used a prefix match or loose comparison
    would cross-kill sessions between users or upstreams.
    """
    mgr = _make_manager()
    alice_notion = _seed_user(mgr, "notion", "alice@example.com")
    alice_slack = _seed_user(mgr, "slack", "alice@example.com")
    bob_notion = _seed_user(mgr, "notion", "bob@example.com")

    await mgr.disconnect_user_session("notion", "alice@example.com")

    alice_notion.close.assert_awaited_once()
    alice_slack.close.assert_not_awaited()
    bob_notion.close.assert_not_awaited()
    assert mgr.has_user_session("notion", "alice@example.com") is False
    assert mgr.has_user_session("slack", "alice@example.com") is True
    assert mgr.has_user_session("notion", "bob@example.com") is True


@pytest.mark.asyncio
async def test_disconnect_user_session_noop_when_missing() -> None:
    """Disconnecting a session that isn't there must be a silent
    no-op, not an error — callers rely on it as cleanup."""
    mgr = _make_manager()
    await mgr.disconnect_user_session("notion", "ghost@example.com")
    # No exception, no state mutation.
    assert mgr._user_sessions == {}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_disconnect_all_user_sessions_scopes_to_one_user() -> None:
    """Removes every ``(user, *)`` tuple for the target user, leaves
    every other user's sessions intact. Returns the count closed."""
    mgr = _make_manager()
    alice_notion = _seed_user(mgr, "notion", "alice@example.com")
    alice_slack = _seed_user(mgr, "slack", "alice@example.com")
    bob_notion = _seed_user(mgr, "notion", "bob@example.com")

    closed = await mgr.disconnect_all_user_sessions("alice@example.com")

    assert closed == 2
    alice_notion.close.assert_awaited_once()
    alice_slack.close.assert_awaited_once()
    bob_notion.close.assert_not_awaited()
    assert mgr.has_user_session("notion", "alice@example.com") is False
    assert mgr.has_user_session("slack", "alice@example.com") is False
    assert mgr.has_user_session("notion", "bob@example.com") is True


@pytest.mark.asyncio
async def test_disconnect_all_user_sessions_does_not_touch_shared() -> None:
    """Per-user teardown must not affect shared sessions."""
    mgr = _make_manager()
    shared = _seed_shared(mgr, "notion")
    _seed_user(mgr, "notion", "alice@example.com")

    await mgr.disconnect_all_user_sessions("alice@example.com")

    shared.close.assert_not_awaited()
    assert mgr.is_connected("notion") is True  # shared still up


# ── disconnect_upstream error tolerance ──────────────────────────────
#
# If ``task.close()`` raises for any reason (transport already half-
# closed, anyio cancel scope race, etc.), the dict state must still be
# consistent afterwards. Leaving entries behind is how "phantom" admin
# sessions survive a restart and block fresh OAuth rounds.


@pytest.mark.asyncio
async def test_disconnect_upstream_clears_state_even_if_close_raises() -> None:
    mgr = _make_manager()
    shared_task = _seed_shared(mgr, "notion")
    shared_task.close.side_effect = RuntimeError("transport died")

    await mgr.disconnect_upstream("notion")

    state = mgr.get_state("notion")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    assert state.shared_session is None
    assert state.shared_task is None


# ── transition_to_disabled invalidates persisted live ref ────────────
#
# When the upstream sits in DEFERRED_ATTACH (no in-memory task, but a
# persistence ref points at a live sandbox + pid from a prior boot),
# ``_drain_state_resources`` has nothing to close — so the
# ``_session_cm.finally`` cleanup that normally kills the sandbox and
# drops the persistence ref never fires. Without an explicit Stop
# → kill_persisted_session call, the next Start's reuse-on-restart
# Path 2 reattaches to the same sandbox and the user sees an empty
# Server-logs panel (the per-session ``LogBuffer.clear()`` runs but
# no fresh install / startup output replaces it).


def _fake_sandbox_service(name: str = "e2b") -> MagicMock:
    """A minimal ``SandboxService`` stand-in tracking
    ``kill_persisted_session`` invocations."""
    svc = MagicMock(name=f"SandboxService.{name}")
    svc.kill_persisted_session = AsyncMock()
    svc.on_upstream_removed = AsyncMock()
    # Return a real string (not a MagicMock) so any path that resolves
    # ``${HOME}`` via this fake gets a usable home value.
    svc.sandbox_home = MagicMock(return_value="/home/user")
    return svc


@pytest.mark.asyncio
async def test_transition_to_disabled_kills_persisted_session() -> None:
    """The user-facing Stop button must invalidate the persistence
    ref so the next Start cold-creates instead of reattaching."""
    fake_e2b = _fake_sandbox_service("e2b")
    mgr = UpstreamClientManager(
        upstreams=[],
        sandbox_services={"e2b": fake_e2b},  # pyright: ignore[reportArgumentType]
    )

    await mgr.transition_to_disabled("notion")

    fake_e2b.kill_persisted_session.assert_awaited_once_with(
        org_id="default", upstream_id="notion",
    )


@pytest.mark.asyncio
async def test_transition_to_disabled_fans_out_to_every_provider() -> None:
    """Same dispatch shape as ``cleanup_sandbox_state_for_upstream``:
    every registered backend is asked, so a stale ref left behind by
    a prior provider switch still gets cleaned up."""
    fake_e2b = _fake_sandbox_service("e2b")
    fake_local = _fake_sandbox_service("local-subprocess")
    mgr = UpstreamClientManager(
        upstreams=[],
        sandbox_services={  # pyright: ignore[reportArgumentType]
            "e2b": fake_e2b, "local-subprocess": fake_local,
        },
    )

    await mgr.disconnect_upstream("github")

    fake_e2b.kill_persisted_session.assert_awaited_once()
    fake_local.kill_persisted_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_transition_to_disabled_swallows_kill_persisted_failure() -> None:
    """A transient SDK error from one backend must not block the
    disable transition — eventual consistency via the reconciler
    is the safety net."""
    fake_e2b = _fake_sandbox_service("e2b")
    fake_e2b.kill_persisted_session.side_effect = RuntimeError("E2B 503")
    mgr = UpstreamClientManager(
        upstreams=[],
        sandbox_services={"e2b": fake_e2b},  # pyright: ignore[reportArgumentType]
    )

    # Must not raise.
    await mgr.transition_to_disabled("slack")

    state = mgr.get_state("slack")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED


# ── Idle sweep: must not touch __admin__ sessions ────────────────────
#
# The sweep is the right policy for real per-user sessions (alice logs
# in once, idles for 30 min, we release the resources). But the
# ``__admin__`` session is upstream-level, not per-user — it's the
# shared backing for ``is_connected()`` and admin-initiated MCP
# calls. For a ``per_user_oauth`` upstream, nothing in normal traffic
# refreshes its ``last_used`` (the router routes users through their
# own ``effective_user``), so without the architectural separation
# the sweep would tear it down at 30 min with no disconnect reason
# written — every user then sees "disconnected" in the UI with no
# "re-authenticate needed" hint.


@pytest.mark.asyncio
async def test_idle_sweep_does_not_tear_down_admin_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcpolis.adapters.upstream_clients import client_manager as cm_module

    # Force every tracked session to register as "idle beyond the
    # threshold" without waiting in real time.
    monkeypatch.setattr(cm_module, "USER_SESSION_IDLE_TIMEOUT", -1)

    mgr = _make_manager()
    admin_task = _seed_user(mgr, "notion", ADMIN_USER_ID)
    alice_task = _seed_user(mgr, "notion", "alice@example.com")

    await mgr._sweep_idle_sessions()  # pyright: ignore[reportPrivateUsage]

    # Admin session survives; regular user session gets reaped.
    assert mgr.has_user_session("notion", ADMIN_USER_ID) is True
    admin_task.close.assert_not_awaited()
    assert mgr.has_user_session("notion", "alice@example.com") is False
    alice_task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_sweep_keeps_admin_even_when_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin-only case is the one that bit us: for per_user_oauth
    upstreams, the admin session is often the only per-user entry, so
    a regression would silently clear the entire map."""
    from mcpolis.adapters.upstream_clients import client_manager as cm_module

    monkeypatch.setattr(cm_module, "USER_SESSION_IDLE_TIMEOUT", -1)

    mgr = _make_manager()
    admin_task = _seed_user(mgr, "mixpanel", ADMIN_USER_ID)

    await mgr._sweep_idle_sessions()  # pyright: ignore[reportPrivateUsage]

    assert mgr.has_user_session("mixpanel", ADMIN_USER_ID) is True
    admin_task.close.assert_not_awaited()
    # And is_connected still reports True — the UI gate the sweep
    # regression was silently flipping to False.
    assert mgr.is_connected("mixpanel") is True


# ── start_all + ensure_shared_connected: the lazy-connect path ───────
#
# Boot must NOT call ``connect_shared`` for a service-account stdio
# upstream when persistence carries cached metadata, because
# ``connect_shared`` opens a streaming RPC to the sandbox and any
# such open call wakes a paused E2B sandbox via auto_resume — the
# very wakeup the persisted-id mechanism exists to avoid. Tool
# dispatch then lazily fires the connect on actual demand.


def _seeded_persistence_with_metadata(
    org_id: str, upstream_id: str,
):  # type: ignore[no-untyped-def]
    """Build an in-memory persistence repo holding a ref with cached
    server_info + self_description — the post-first-boot steady state.
    """
    from datetime import UTC, datetime

    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )
    from mcpolis.domain.model.upstream import (
        ServerInfo,
        UpstreamSelfDescription,
    )
    from mcpolis.domain.ports.sandbox_persistence_repository import (
        SandboxPersistedRef,
    )

    persistence = InMemorySandboxPersistenceRepository()
    ref = SandboxPersistedRef(
        provider="e2b",
        org_id=org_id,
        upstream_id=upstream_id,
        mcpolis_instance="prior-instance",
        sandbox_id="sbx-survived",
        paused_snapshot_id=None,
        pid=4242,
        metadata={},
        cached_server_info=ServerInfo(name="cached-server", version="1.0.0"),
        cached_self_description=UpstreamSelfDescription(
            name="cached-server", version="1.0.0",
        ),
        last_updated=datetime.now(UTC),
    )
    return persistence, ref


@pytest.mark.asyncio
async def test_start_all_skips_connect_when_metadata_cached() -> None:
    """The headline lazy-connect assertion at unit scope: if the
    persisted ref carries cached_server_info + cached_self_description,
    ``start_all`` MUST skip ``connect_shared`` for the upstream and
    populate the dashboard caches from persistence instead.
    """
    from tests.unit.factories import make_upstream_definition

    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence, _ref = _seeded_persistence_with_metadata(org_id, upstream.id)
    await persistence.upsert(_ref)

    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    mgr.connect_shared = AsyncMock()  # type: ignore[method-assign]

    await mgr.start_all()

    mgr.connect_shared.assert_not_awaited()
    # Dashboard reads come from cache without a live session.
    server_info = mgr.get_server_info(upstream.id)
    assert server_info is not None
    assert server_info.name == "cached-server"
    assert mgr.get_self_description(upstream.id) is not None


@pytest.mark.asyncio
async def test_start_all_eagerly_connects_when_no_cache() -> None:
    """The boot-skip is gated on cached metadata existing. When the
    persisted ref has no cache (first boot, or pre-feature ref),
    ``start_all`` falls back to the eager-connect path so the cache
    populates for the next restart.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )
    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(id="no-cache-stdio")
    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id="acme",
        sandbox_persistence=InMemorySandboxPersistenceRepository(),
    )
    mgr.connect_shared = AsyncMock()  # type: ignore[method-assign]

    await mgr.start_all()

    mgr.connect_shared.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_shared_connected_noop_when_session_present() -> None:
    """``ensure_shared_connected`` must be cheap when there's already
    a session: no ``connect_shared`` call, no E2B-side wake."""
    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(id="already-live")
    mgr = _make_manager()
    _seed_shared(mgr, upstream.id)
    mgr.connect_shared = AsyncMock()  # type: ignore[method-assign]

    await mgr.ensure_shared_connected(upstream)

    mgr.connect_shared.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_shared_connected_invokes_connect_when_no_session() -> None:
    """The inverse: when boot deferred the connect, the lazy path
    fires ``connect_shared`` exactly once."""
    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(id="lazy-target")
    mgr = _make_manager()
    mgr.connect_shared = AsyncMock()  # type: ignore[method-assign]

    await mgr.ensure_shared_connected(upstream)

    mgr.connect_shared.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_connected_true_for_deferred_attach_upstream() -> None:
    """Deferred-attach is "ready" from the user's POV — the cached
    metadata satisfies dashboard reads and ``ensure_shared_connected``
    reattaches lazily on first tool call. ``is_connected`` MUST
    reflect that, otherwise the dashboard renders cached upstreams
    as "Stopped" / "Not started" which scares operators into
    clicking reconnect (and waking the very sandbox we just paused).
    """
    from tests.unit.factories import make_upstream_definition
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence, ref = _seeded_persistence_with_metadata(org_id, upstream.id)
    await persistence.upsert(ref)

    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    assert mgr.is_connected(upstream.id) is False

    deferred = await mgr.connect_shared_or_defer(upstream)
    assert deferred is True
    assert mgr.is_connected(upstream.id) is True, (
        "is_connected must return True for a deferred-attach upstream "
        "— the dashboard's readiness badge reads from this gate"
    )


@pytest.mark.asyncio
async def test_ready_upstream_ids_includes_deferred_attach() -> None:
    """``ready_upstream_ids`` is the user-facing readiness accessor —
    it MUST include deferred-attach upstreams, while
    ``connected_upstream_ids`` keeps its narrower live-session
    semantic (used by ``ToolRegistry.refresh_all`` to avoid
    eagerly waking paused sandboxes during catalog refresh).
    """
    from tests.unit.factories import make_upstream_definition
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence, ref = _seeded_persistence_with_metadata(org_id, upstream.id)
    await persistence.upsert(ref)

    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    await mgr.connect_shared_or_defer(upstream)

    # The user-facing accessor includes deferred upstreams.
    assert upstream.id in mgr.ready_upstream_ids
    # The narrow live-session accessor does NOT — refresh_all uses
    # this and must not refresh a deferred upstream (would wake it).
    assert upstream.id not in mgr.connected_upstream_ids


@pytest.mark.asyncio
async def test_disconnect_upstream_clears_deferred_attach_state() -> None:
    """Admin "Disconnect" means "this upstream is not ready right
    now" — drop the deferred-attach placeholder so the readiness
    gate reflects the disconnected state instead of leaving a stale
    "ready (deferred)" entry."""
    from tests.unit.factories import make_upstream_definition
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence, ref = _seeded_persistence_with_metadata(org_id, upstream.id)
    await persistence.upsert(ref)

    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    await mgr.connect_shared_or_defer(upstream)
    assert mgr.is_connected(upstream.id) is True

    await mgr.disconnect_upstream(upstream.id)

    assert mgr.is_connected(upstream.id) is False, (
        "after admin disconnect, is_connected should be False; the "
        "deferred-attach placeholder must be dropped"
    )


@pytest.mark.asyncio
async def test_ensure_shared_connected_replaces_deferred_with_live_session() -> None:
    """Once the lazy ensure has opened a real session, the
    deferred-attach placeholder is redundant and must be cleared so
    the two signals don't drift."""
    from tests.unit.factories import make_upstream_definition
    org_id = "acme"
    upstream = make_upstream_definition(id="cached-stdio")
    persistence, ref = _seeded_persistence_with_metadata(org_id, upstream.id)
    await persistence.upsert(ref)

    mgr = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_persistence=persistence,
    )
    await mgr.connect_shared_or_defer(upstream)
    state_before = mgr.get_state(upstream.id)
    assert state_before is not None
    assert state_before.state == UpstreamConnectionState.DEFERRED_ATTACH

    # Simulate a successful lazy connect: stub out the actual session
    # creation but route the housekeeping through the public
    # state-machine API (which is what advances DEFERRED_ATTACH → LIVE).
    async def _fake_connect(
        u: object, bearer_token: object = None, auth: object = None,
    ) -> None:
        del u, bearer_token, auth
        mgr.transition_to_live_shared(
            upstream.id,
            session=_stub_session(),
            task=_stub_task(),
            server_info=None,
            self_description=None,
        )

    mgr.connect_shared = _fake_connect  # type: ignore[method-assign,assignment]

    await mgr.ensure_shared_connected(upstream)

    state_after = mgr.get_state(upstream.id)
    assert state_after is not None
    assert state_after.state == UpstreamConnectionState.LIVE, (
        "after lazy connect succeeds, deferred-attach placeholder "
        "should be cleared in favour of the live session"
    )
    assert mgr.is_connected(upstream.id) is True


@pytest.mark.asyncio
async def test_ensure_shared_connected_single_flight() -> None:
    """Concurrent callers must NOT each fire their own ``Sandbox.connect``
    — the second caller awaits the first's task. Single-flight is the
    whole point of ``_lazy_connect_tasks`` (parallel tool calls on the
    same upstream would otherwise stampede E2B with N wakes).
    """
    import asyncio as _asyncio

    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(id="contended")
    mgr = _make_manager()

    started = _asyncio.Event()
    proceed = _asyncio.Event()
    call_count = 0

    async def _slow_connect(*_args: Any, **_kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        started.set()
        await proceed.wait()
        # Simulate a successful session open through the state machine.
        mgr.transition_to_live_shared(
            upstream.id,
            session=_stub_session(),
            task=_stub_task(),
            server_info=None,
            self_description=None,
        )

    mgr.connect_shared = _slow_connect  # type: ignore[method-assign,assignment]

    first = _asyncio.create_task(mgr.ensure_shared_connected(upstream))
    await started.wait()
    second = _asyncio.create_task(mgr.ensure_shared_connected(upstream))

    # Let both finish.
    proceed.set()
    await _asyncio.gather(first, second)

    assert call_count == 1, (
        f"connect_shared fired {call_count}× — concurrent "
        f"ensure_shared_connected calls stampeded the connect"
    )


@pytest.mark.asyncio
async def test_is_starting_tracks_background_connect_task() -> None:
    """``is_starting`` is the dashboard's "Starting…" gate. It
    must be True while a fire-and-forget reconnect task is in
    flight (admin clicked Start, background ``connect_shared``
    is running) and flip to False the moment the task completes —
    success OR failure. Replaces the old ``sandbox_state`` signal
    that came from the deleted lifecycle pill / state registry.
    """
    import asyncio as _asyncio

    mgr = _make_manager()

    # No background task → not starting.
    assert mgr.is_starting("nope") is False

    # Register an in-flight task.
    proceed = _asyncio.Event()

    async def _bg() -> None:
        await proceed.wait()

    task = _asyncio.create_task(_bg())
    mgr.register_background_connect_task("slack", task)

    assert mgr.is_starting("slack") is True

    # Let it complete; the registered done-callback clears the slot.
    proceed.set()
    await task
    # Yield once so the done_callback fires.
    await _asyncio.sleep(0)

    assert mgr.is_starting("slack") is False


# ── Idle sweep loop wiring ──────────────────────────────────────────
#
# The earlier idle-sweep tests call ``_sweep_idle_sessions`` directly,
# which pins the *selection* logic but not the *loop wiring*
# (``start_idle_sweep`` → ``asyncio.sleep(USER_SESSION_SWEEP_INTERVAL)``
# → ``_sweep_idle_sessions``). If a future refactor drops the
# ``start_idle_sweep`` call site or breaks the loop body, the
# selection tests stay green but production has a silently-disabled
# sweep — every per-user session lives forever. This test drives the
# real loop end-to-end with a tiny interval and verifies one tick
# actually reaps the right session.


@pytest.mark.asyncio
async def test_start_idle_sweep_drives_real_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcpolis.adapters.upstream_clients import client_manager as cm_module

    # Tiny interval so one tick fires within the test window; idle
    # threshold negative so every tracked session qualifies for reap.
    monkeypatch.setattr(cm_module, "USER_SESSION_SWEEP_INTERVAL", 0.01)
    monkeypatch.setattr(cm_module, "USER_SESSION_IDLE_TIMEOUT", -1)

    mgr = _make_manager()
    alice_task = _seed_user(mgr, "notion", "alice@example.com")

    mgr.start_idle_sweep()
    try:
        # Wait up to ~0.5s for one sweep tick to land. Polling rather
        # than a fixed sleep keeps the test fast in the common case
        # while still tolerating loop-scheduling jitter.
        for _ in range(50):
            if not mgr.has_user_session("notion", "alice@example.com"):
                break
            await asyncio.sleep(0.01)

        assert mgr.has_user_session("notion", "alice@example.com") is False, (
            "start_idle_sweep loop did not reap an idle session within "
            "the timeout — loop wiring is broken"
        )
        alice_task.close.assert_awaited_once()
    finally:
        # Stop the sweep so it doesn't outlive the test event loop.
        sweep_task = mgr._sweep_task  # pyright: ignore[reportPrivateUsage]
        if sweep_task is not None:
            sweep_task.cancel()
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass


# ── Concurrent connect_upstream_for_user for same (user, upstream) ──
#
# Without per-key serialization, two concurrent callers race their
# disconnect+create sequences. The losing caller's session is leaked:
# it's overwritten in ``_user_sessions[key]`` / ``_user_tasks[key]``
# without anyone awaiting its ``task.close()``. Replace semantics
# (callers expect "the latest connect wins, prior session torn down
# cleanly") demand that the second caller's ``disconnect_user_session``
# observes and closes the first caller's task.


@pytest.mark.asyncio
async def test_concurrent_connect_upstream_for_user_does_not_leak() -> None:
    """Concurrent calls for the same ``(user, upstream)`` must
    serialize: the second caller's disconnect closes the first
    caller's task (replace semantics). Without serialization the
    first task is silently leaked.
    """
    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(
        id="contended-user",
        transport=TransportType.streamable_http,
    )
    mgr = _make_manager()

    created_tasks: list[MagicMock] = []
    create_count = 0
    started_first = asyncio.Event()
    proceed_first = asyncio.Event()

    async def _slow_create(
        upstream_arg: Any,
        user_id: str,
        bearer_token: str | None = None,
        auth: Any = None,
    ) -> tuple[Any, MagicMock]:
        del upstream_arg, user_id, bearer_token, auth
        nonlocal create_count
        create_count += 1
        is_first = create_count == 1
        new_task = _stub_task()
        created_tasks.append(new_task)
        if is_first:
            started_first.set()
            await proceed_first.wait()
        return _stub_session(), new_task

    mgr._create_task = _slow_create  # type: ignore[method-assign,assignment]

    user = "alice@example.com"
    first = asyncio.create_task(
        mgr.connect_upstream_for_user(upstream, user)
    )
    await started_first.wait()
    second = asyncio.create_task(
        mgr.connect_upstream_for_user(upstream, user)
    )
    # Yield so `second` makes as much progress as it can. With
    # serialization it blocks; without, it races into _create_task.
    await asyncio.sleep(0)

    proceed_first.set()
    await asyncio.gather(first, second)

    # Both callers ran (replace semantics — each requested a fresh
    # session for that user). The contract being pinned: the FIRST
    # caller's task got closed when the second's disconnect ran,
    # rather than being silently overwritten in the dict.
    assert create_count == 2
    created_tasks[0].close.assert_awaited_once()
    created_tasks[1].close.assert_not_awaited()
    key = (user, upstream.id)
    assert (
        mgr._user_tasks[key] is created_tasks[1]  # pyright: ignore[reportPrivateUsage]
    )


# ── disconnect_all_user_sessions: count contract under failure ──────
#
# ``disconnect_all_user_sessions`` returns the count of sessions
# torn down. The contract being pinned: the count reflects keys
# REMOVED FROM MANAGER STATE, not transports SUCCESSFULLY CLOSED.
# Callers (admin user-removal, sign-out flow) use the count for
# audit logging — they care about "how many sessions did the
# manager forget about", which a partial close failure must NOT
# under-report. The underlying transport-close failure is logged
# and swallowed by ``disconnect_user_session``; the count keeps
# its "attempted" semantic.


@pytest.mark.asyncio
async def test_disconnect_all_user_sessions_counts_close_failures() -> None:
    mgr = _make_manager()
    alice_notion = _seed_user(mgr, "notion", "alice@example.com")
    alice_slack = _seed_user(mgr, "slack", "alice@example.com")
    alice_notion.close.side_effect = RuntimeError("transport already gone")

    closed = await mgr.disconnect_all_user_sessions("alice@example.com")

    # The count includes the failing key — callers rely on it as
    # "how many sessions did we drop from state".
    assert closed == 2
    # State is fully cleared regardless of close-failure.
    assert mgr.has_user_session("notion", "alice@example.com") is False
    assert mgr.has_user_session("slack", "alice@example.com") is False
    # Both close() attempts were made (the failing one isn't skipped).
    alice_notion.close.assert_awaited_once()
    alice_slack.close.assert_awaited_once()
