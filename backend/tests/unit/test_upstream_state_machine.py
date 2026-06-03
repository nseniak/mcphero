"""Pin the ``UpstreamClientManager`` state-machine transitions.

Every transition method (``transition_to_disabled``,
``transition_to_failed``, ``transition_to_deferred_attach``,
``transition_to_connecting``, ``transition_to_live_shared``,
``transition_to_live_admin``) is exercised here for:

- the resulting ``state.state`` enum,
- which slots (sessions, tasks, metadata, background_task,
  last_failure) are preserved vs cleared,
- side effects on the OLD state record (close awaits + cancellation).

Plus the close-inplace helpers (``_close_shared_inplace`` /
``_close_admin_inplace``) — they're not transitions in the strict
sense (they recompute the resulting phase from what's left) but
they're load-bearing for ``connect_shared`` / ``disconnect_upstream``
correctness, so the recompute rule needs explicit tests.

These are unit tests at the manager surface — they don't touch any
session transport or sandbox. The real lifecycle is exercised in
``test_client_manager_lifecycle.py`` (public-API tests) and the
e2b integration suite.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.upstream_state import (
    UpstreamConnectionState,
    UpstreamState,
)
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)


def _mgr() -> UpstreamClientManager:
    return UpstreamClientManager(upstreams=[])


def _stub_session(name: str = "session") -> Any:
    return MagicMock(name=name)


def _stub_task(name: str = "task") -> MagicMock:
    task = MagicMock(name=name)
    task.close = AsyncMock()
    task.server_info = None
    task.self_description = None
    return task


def _server_info() -> ServerInfo:
    return ServerInfo(name="x", version="1.0.0")


def _self_description() -> UpstreamSelfDescription:
    return UpstreamSelfDescription(name="x", version="1.0.0")


# ── Constructor / initial state ───────────────────────────────────────


def test_constructor_initializes_each_upstream_to_failed_none() -> None:
    """Every registered upstream starts in FAILED with
    last_failure=None — "registered, never connected." This is the
    invariant downstream readers (``is_connected``, ``ready_upstream_ids``)
    rely on for upstreams that haven't been touched yet."""
    from tests.unit.factories import make_upstream_definition
    upstreams = [
        make_upstream_definition(id="a"),
        make_upstream_definition(id="b"),
    ]
    mgr = UpstreamClientManager(upstreams=upstreams)
    for uid in ("a", "b"):
        state = mgr.get_state(uid)
        assert state is not None
        assert state.state == UpstreamConnectionState.FAILED
        assert state.last_failure is None
        assert state.shared_session is None
        assert state.admin_session is None
        assert state.background_task is None


def test_register_upstream_creates_state_record_lazily() -> None:
    from tests.unit.factories import make_upstream_definition
    mgr = _mgr()
    upstream = make_upstream_definition(id="late")
    mgr.register_upstream(upstream)
    state = mgr.get_state("late")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED


# ── transition_to_disabled ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_drops_sessions_and_metadata() -> None:
    mgr = _mgr()
    shared_task = _stub_task("shared")
    admin_task = _stub_task("admin")
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=shared_task,
        server_info=_server_info(), self_description=_self_description(),
    )
    mgr.transition_to_live_admin(
        "u", session=_stub_session(), task=admin_task,
        server_info=_server_info(), self_description=_self_description(),
    )
    assert mgr.get_state("u").state == UpstreamConnectionState.LIVE  # type: ignore[union-attr]

    await mgr.transition_to_disabled("u")

    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    # Everything dropped — DISABLED is a clean slate, even cached
    # metadata is lost (admin Stop = "this upstream is not ready").
    assert state.shared_session is None
    assert state.shared_task is None
    assert state.admin_session is None
    assert state.admin_task is None
    assert state.server_info is None
    assert state.self_description is None
    assert state.background_task is None
    # The sessions' tasks were closed.
    shared_task.close.assert_awaited_once()
    admin_task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_with_last_failure_records_context() -> None:
    """Auto-disable-on-failure carries the failure reason on the
    DISABLED record so the dashboard can surface it."""
    mgr = _mgr()
    await mgr.transition_to_disabled(
        "u", last_failure="boom", reason="auto_disable_on_failure",
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    assert state.last_failure == "boom"


@pytest.mark.asyncio
async def test_disabled_cancels_in_flight_background_task() -> None:
    """Admin Stop while a Reconnect is in flight must cancel and
    await the background task before declaring DISABLED. Otherwise
    the still-running connect can re-register a session right after
    teardown."""
    mgr = _mgr()
    proceed = asyncio.Event()

    async def _bg() -> None:
        await proceed.wait()

    bg = asyncio.create_task(_bg())
    mgr.transition_to_connecting("u", background_task=bg)
    assert mgr.is_starting("u") is True

    await mgr.transition_to_disabled("u")

    assert bg.cancelled() or bg.done()
    assert mgr.is_starting("u") is False


# ── transition_to_failed ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_preserves_metadata_for_retry_visibility() -> None:
    """A transient connect failure shouldn't lose the cached
    metadata — the next attempt can render the dashboard from cache
    while retrying."""
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=_server_info(), self_description=_self_description(),
    )

    await mgr.transition_to_failed("u", last_failure="oops")

    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.last_failure == "oops"
    # Sessions dropped.
    assert state.shared_session is None
    assert state.admin_session is None
    # Metadata preserved.
    assert state.server_info is not None
    assert state.self_description is not None


@pytest.mark.asyncio
async def test_failed_with_no_last_failure_means_never_attempted() -> None:
    """``last_failure=None`` distinguishes "registered, never
    connected" from "we tried and got X" — both are FAILED for the
    user but the dashboard renders them differently."""
    mgr = _mgr()
    await mgr.transition_to_failed("u", last_failure=None, reason="never_attempted")
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.last_failure is None


# ── transition_to_deferred_attach ─────────────────────────────────────


@pytest.mark.asyncio
async def test_deferred_attach_sets_metadata_and_drops_sessions() -> None:
    mgr = _mgr()
    shared_task = _stub_task("shared")
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=shared_task,
        server_info=None, self_description=None,
    )

    await mgr.transition_to_deferred_attach(
        "u",
        server_info=_server_info(),
        self_description=_self_description(),
    )

    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DEFERRED_ATTACH
    assert state.server_info is not None
    assert state.self_description is not None
    assert state.shared_session is None
    assert state.shared_task is None
    shared_task.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_attach_makes_is_connected_true() -> None:
    """The whole point of DEFERRED_ATTACH: ``is_connected`` returns
    True so the dashboard renders Ready, while no sandbox has been
    woken."""
    mgr = _mgr()
    await mgr.transition_to_deferred_attach(
        "u",
        server_info=_server_info(),
        self_description=_self_description(),
    )
    assert mgr.is_connected("u") is True
    assert "u" in mgr.ready_upstream_ids
    # connected_upstream_ids is the *narrow* live-session accessor —
    # MUST exclude DEFERRED_ATTACH so refresh_all doesn't wake the
    # paused sandbox.
    assert "u" not in mgr.connected_upstream_ids


# ── transition_to_connecting ──────────────────────────────────────────


def test_connecting_preserves_cached_metadata() -> None:
    """Admin Reconnect on a previously-cached upstream: dashboard
    keeps showing the cached server_info while reconnecting, so the
    Starting… UI doesn't lose context."""
    mgr = _mgr()

    async def _bg() -> None:
        await asyncio.sleep(60)

    asyncio.set_event_loop(asyncio.new_event_loop())
    bg = asyncio.get_event_loop().create_task(_bg())
    try:
        # Seed cached metadata via DEFERRED_ATTACH first.
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            mgr.transition_to_deferred_attach(
                "u",
                server_info=_server_info(),
                self_description=_self_description(),
            ),
        )
        mgr.transition_to_connecting("u", background_task=bg)
        state = mgr.get_state("u")
        assert state is not None
        assert state.state == UpstreamConnectionState.CONNECTING
        assert state.server_info is not None
        assert state.self_description is not None
        assert state.background_task is bg
    finally:
        bg.cancel()


@pytest.mark.asyncio
async def test_connecting_clears_stale_last_failure() -> None:
    """Admin clicked Start after a prior FAILED — the new attempt
    shouldn't carry the stale failure context (otherwise the
    dashboard surfaces "Reconnecting (last failure: X)" with X from
    the LAST attempt, misleading the operator)."""
    mgr = _mgr()
    await mgr.transition_to_failed("u", last_failure="prior boom")

    async def _bg() -> None:
        await asyncio.sleep(60)

    bg = asyncio.create_task(_bg())
    try:
        mgr.transition_to_connecting("u", background_task=bg)
        state = mgr.get_state("u")
        assert state is not None
        assert state.last_failure is None
    finally:
        bg.cancel()


@pytest.mark.asyncio
async def test_connecting_cancels_prior_background_task() -> None:
    """Re-clicking Start while a prior reconnect is in flight must
    cancel the prior task — racing two warming sandboxes is wasteful
    and the second click is the operator's intent."""
    mgr = _mgr()

    async def _bg() -> None:
        await asyncio.sleep(60)

    first = asyncio.create_task(_bg())
    second = asyncio.create_task(_bg())
    try:
        mgr.transition_to_connecting("u", background_task=first)
        mgr.transition_to_connecting("u", background_task=second)
        # First was cancelled when second registered.
        await asyncio.sleep(0)  # yield so cancellation propagates
        assert first.cancelled() or first.done()
        # Second is still in flight.
        assert mgr.is_starting("u") is True
    finally:
        first.cancel()
        second.cancel()


# ── transition_to_live_shared ─────────────────────────────────────────


def test_live_shared_sets_session_and_task_marks_live() -> None:
    mgr = _mgr()
    sess = _stub_session("shared")
    task = _stub_task("shared")
    mgr.transition_to_live_shared(
        "u", session=sess, task=task,
        server_info=_server_info(),
        self_description=_self_description(),
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.shared_session is sess
    assert state.shared_task is task
    assert state.server_info is not None
    assert state.self_description is not None


def test_live_shared_preserves_existing_admin_session() -> None:
    """OAuth upstreams can have BOTH a shared discovery session AND
    an admin OAuth session simultaneously. Adding a shared MUST
    preserve the admin slot."""
    mgr = _mgr()
    admin_sess = _stub_session("admin")
    admin_task = _stub_task("admin")
    mgr.transition_to_live_admin(
        "u", session=admin_sess, task=admin_task,
        server_info=None, self_description=None,
    )
    mgr.transition_to_live_shared(
        "u",
        session=_stub_session("shared"),
        task=_stub_task("shared"),
        server_info=None, self_description=None,
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.shared_session is not None
    assert state.admin_session is admin_sess
    assert state.admin_task is admin_task


def test_live_shared_clears_background_task_and_last_failure() -> None:
    """Successful connect → drop stale CONNECTING/FAILED context."""
    mgr = _mgr()

    async def _bg() -> None:
        await asyncio.sleep(60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bg = loop.create_task(_bg())
    try:
        mgr.transition_to_connecting("u", background_task=bg)
        # Now connect succeeds.
        mgr.transition_to_live_shared(
            "u",
            session=_stub_session(),
            task=_stub_task(),
            server_info=None, self_description=None,
        )
        state = mgr.get_state("u")
        assert state is not None
        assert state.state == UpstreamConnectionState.LIVE
        assert state.background_task is None
        assert state.last_failure is None
    finally:
        bg.cancel()
        # Drain the cancellation so the coroutine isn't GC'd unawaited.
        try:
            loop.run_until_complete(bg)
        except asyncio.CancelledError:
            pass
        loop.close()


def test_live_shared_overrides_metadata_from_new_task_when_provided() -> None:
    """A successful reconnect with fresh server_info overwrites the
    cached version — the live data wins."""
    mgr = _mgr()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(
        mgr.transition_to_deferred_attach(
            "u",
            server_info=ServerInfo(name="cached", version="0.1.0"),
            self_description=UpstreamSelfDescription(
                name="cached", version="0.1.0",
            ),
        ),
    )
    fresh_si = ServerInfo(name="fresh", version="2.0.0")
    fresh_sd = UpstreamSelfDescription(name="fresh", version="2.0.0")
    mgr.transition_to_live_shared(
        "u",
        session=_stub_session(),
        task=_stub_task(),
        server_info=fresh_si,
        self_description=fresh_sd,
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.server_info == fresh_si
    assert state.self_description == fresh_sd


def test_live_shared_preserves_metadata_when_new_task_lacks_it() -> None:
    """Some connect paths don't return server_info on the task
    (e.g. cached fallthroughs). When new info is None, the cached
    one survives — losing it would force the dashboard to render
    "Loading…" between LIVE flips."""
    mgr = _mgr()
    asyncio.set_event_loop(asyncio.new_event_loop())
    asyncio.get_event_loop().run_until_complete(
        mgr.transition_to_deferred_attach(
            "u",
            server_info=_server_info(),
            self_description=_self_description(),
        ),
    )
    mgr.transition_to_live_shared(
        "u",
        session=_stub_session(),
        task=_stub_task(),
        server_info=None,
        self_description=None,
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.server_info is not None
    assert state.self_description is not None


# ── transition_to_live_admin ──────────────────────────────────────────


def test_live_admin_sets_session_and_task_marks_live() -> None:
    mgr = _mgr()
    sess = _stub_session("admin")
    task = _stub_task("admin")
    mgr.transition_to_live_admin(
        "u", session=sess, task=task,
        server_info=None, self_description=None,
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.admin_session is sess
    assert state.admin_task is task
    assert state.shared_session is None  # admin-only LIVE


def test_live_admin_preserves_existing_shared_session() -> None:
    """Symmetric to ``test_live_shared_preserves_existing_admin_session``."""
    mgr = _mgr()
    shared_sess = _stub_session("shared")
    shared_task = _stub_task("shared")
    mgr.transition_to_live_shared(
        "u", session=shared_sess, task=shared_task,
        server_info=None, self_description=None,
    )
    mgr.transition_to_live_admin(
        "u",
        session=_stub_session("admin"),
        task=_stub_task("admin"),
        server_info=None, self_description=None,
    )
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.shared_session is shared_sess
    assert state.shared_task is shared_task
    assert state.admin_session is not None


# ── _close_shared_inplace recompute ───────────────────────────────────
#
# After dropping the shared session, the resulting state depends on
# what's left:
#   - admin still present → LIVE (admin satisfies "usable")
#   - cached metadata present → DEFERRED_ATTACH (cache satisfies UI)
#   - nothing left → FAILED (or DISABLED if it already was)


@pytest.mark.asyncio
async def test_close_shared_inplace_keeps_live_when_admin_present() -> None:
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=None, self_description=None,
    )
    mgr.transition_to_live_admin(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=None, self_description=None,
    )
    await mgr._close_shared_inplace("u")  # pyright: ignore[reportPrivateUsage]
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.shared_session is None
    assert state.admin_session is not None


@pytest.mark.asyncio
async def test_close_shared_inplace_falls_back_to_deferred_when_metadata_present() -> None:
    """Common case: shared session was the only live channel; cached
    metadata is still around (it was populated when the session
    opened). Drop to DEFERRED_ATTACH so the dashboard keeps
    showing Ready and the next tool dispatch reattaches lazily."""
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=_server_info(), self_description=_self_description(),
    )
    await mgr._close_shared_inplace("u")  # pyright: ignore[reportPrivateUsage]
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DEFERRED_ATTACH


@pytest.mark.asyncio
async def test_close_shared_inplace_falls_back_to_failed_when_nothing_left() -> None:
    """No admin, no metadata → FAILED. last_failure stays None
    (this isn't a connect failure, it's an explicit close)."""
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=None, self_description=None,
    )
    await mgr._close_shared_inplace("u")  # pyright: ignore[reportPrivateUsage]
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.last_failure is None


@pytest.mark.asyncio
async def test_close_shared_inplace_preserves_disabled_state() -> None:
    """If the upstream was DISABLED (admin Stopped it), dropping a
    session must NOT auto-revert to FAILED — DISABLED stays sticky."""
    mgr = _mgr()
    # Explicitly DISABLED, no sessions to begin with.
    await mgr.transition_to_disabled("u")
    # No shared session to close — _close_shared_inplace is a no-op
    # for the DISABLED path. (Adding a shared session post-DISABLE
    # is a separate transition.)
    await mgr._close_shared_inplace("u")  # pyright: ignore[reportPrivateUsage]
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED


@pytest.mark.asyncio
async def test_close_shared_inplace_keeps_connecting_when_bg_task_in_flight() -> None:
    """If a Reconnect is in flight (CONNECTING) when we close the
    old shared, the in-flight task is the source of truth — stay
    CONNECTING, don't fall back."""
    mgr = _mgr()

    async def _bg() -> None:
        await asyncio.sleep(60)

    bg = asyncio.create_task(_bg())
    try:
        mgr.transition_to_live_shared(
            "u", session=_stub_session(), task=_stub_task(),
            server_info=None, self_description=None,
        )
        # Then admin clicks Reconnect; CONNECTING is registered with
        # the background task. The transition preserves the existing
        # session slot; an actual reconnect would close-then-reopen.
        mgr.transition_to_connecting("u", background_task=bg)
        # Now drop the shared (simulating the close-then-open inside
        # the reconnect path).
        await mgr._close_shared_inplace("u")  # pyright: ignore[reportPrivateUsage]
        state = mgr.get_state("u")
        assert state is not None
        assert state.state == UpstreamConnectionState.CONNECTING
    finally:
        bg.cancel()


# ── _close_admin_inplace recompute ────────────────────────────────────
# Symmetric to the shared variants above; one canonical case here.


@pytest.mark.asyncio
async def test_close_admin_inplace_keeps_live_when_shared_present() -> None:
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=None, self_description=None,
    )
    mgr.transition_to_live_admin(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=None, self_description=None,
    )
    await mgr._close_admin_inplace("u")  # pyright: ignore[reportPrivateUsage]
    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.LIVE
    assert state.admin_session is None
    assert state.shared_session is not None


# ── Error tolerance: close raising must not break state ───────────────


@pytest.mark.asyncio
async def test_disabled_swallows_close_error_and_still_advances_state() -> None:
    """If task.close() raises, the state record must still flip to
    DISABLED — leaving stale LIVE entries is exactly how phantom
    sessions end up surviving an admin Stop."""
    mgr = _mgr()
    bad_task = _stub_task()
    bad_task.close.side_effect = RuntimeError("transport died")
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=bad_task,
        server_info=None, self_description=None,
    )

    await mgr.transition_to_disabled("u")

    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.DISABLED
    bad_task.close.assert_awaited_once()


# ── Sanity: state record's last_transition_at advances ────────────────


@pytest.mark.asyncio
async def test_each_transition_advances_last_transition_at() -> None:
    """Operators rely on ``last_transition_at`` to age the record —
    every transition must update it. The transitions don't share a
    timestamp source, so a regression where one path forgot to
    refresh would leave stale times in prod."""
    mgr = _mgr()
    await mgr.transition_to_failed("u", last_failure=None)
    t0 = mgr.get_state("u").last_transition_at  # type: ignore[union-attr]
    # Sleep long enough that even coarse clocks tick.
    await asyncio.sleep(0.001)
    await mgr.transition_to_disabled("u")
    t1 = mgr.get_state("u").last_transition_at  # type: ignore[union-attr]
    assert t1 > t0
    assert t1.tzinfo == UTC


# ── UpstreamState.has_any_session ─────────────────────────────────────


def test_has_any_session_reflects_either_slot() -> None:
    """Quick sanity: the ``has_any_session`` derived predicate is
    used by ``connected_upstream_ids`` to decide what to refresh.
    If it disagrees with the underlying slots, refresh_all could
    skip live upstreams."""
    s = UpstreamState(state=UpstreamConnectionState.FAILED)
    assert s.has_any_session is False

    s.shared_session = _stub_session()
    assert s.has_any_session is True

    s.shared_session = None
    s.admin_session = _stub_session()
    assert s.has_any_session is True


# ── Persistence-of-metadata invariant ─────────────────────────────────


@pytest.mark.asyncio
async def test_failed_after_live_keeps_metadata_for_retry() -> None:
    """The full retry visibility flow: LIVE → FAILED preserves
    server_info AND self_description so the next attempt can
    render the dashboard from cache while ``connect_shared`` is in
    flight."""
    mgr = _mgr()
    mgr.transition_to_live_shared(
        "u", session=_stub_session(), task=_stub_task(),
        server_info=_server_info(), self_description=_self_description(),
    )
    await mgr.transition_to_failed("u", last_failure="oops")

    state = mgr.get_state("u")
    assert state is not None
    assert state.state == UpstreamConnectionState.FAILED
    assert state.server_info is not None
    assert state.self_description is not None
    assert state.last_failure == "oops"


# ── Constructor sanity for an empty manager ───────────────────────────


def test_empty_manager_has_no_state_records() -> None:
    """Tests that build managers with ``upstreams=[]`` rely on
    ``get_state`` returning None for unknown ids."""
    mgr = _mgr()
    assert mgr.get_state("nonexistent") is None
    assert mgr.is_connected("nonexistent") is False
    assert mgr.is_starting("nonexistent") is False
    assert mgr.ready_upstream_ids == []
    assert mgr.connected_upstream_ids == []


# ── _ = datetime suppresses unused-import lint. ───────────────────────
_ = datetime
