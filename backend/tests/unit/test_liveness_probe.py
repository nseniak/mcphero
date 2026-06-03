"""Tests for §5.5 — periodic liveness probe.

Motivation (``internal/documents/oauth-durability.md`` §5.5 / §8): the
``is_connected(upstream_id)`` lookup that backs our "is this MCP
reachable?" UI is a pure dict membership test. A session whose
underlying transport has long since idle-closed server-side, or whose
refresh token has quietly gone bad upstream, still shows "connected"
until the next real tool call. On 2026-04-24 a restart revealed a
Mixpanel session that had been dead for ≥12h — the UI had been
lying because no one had exercised the session in that window.

The probe runs ``list_tools()`` hourly against every live OAuth
session and tears down the ones that no longer respond. These tests
pin three outcomes keyed on what ``list_tools`` returns / raises:

- Success → session left alone; no disconnect call.
- Transient (``asyncio.TimeoutError``, ``httpx.ConnectError``,
  ``OSError``) → log at DEBUG and leave the session alone. A brief
  network glitch during the probe shouldn't tear down a session
  that's otherwise healthy — the next probe or real tool call retries.
- Fatal (any other exception) → tear down the session and call
  ``reconnect_with_stored_tokens``. The §5.1 delete-vs-retry policy
  runs inside that reconnect path, so the probe doesn't re-implement
  it.

The probe is driven with a mocked ``ClientSession`` (an AsyncMock
with a scripted ``list_tools``) plus a real
``UpstreamClientManager`` with a seeded session. Going through the
real client_manager proves the disconnect call actually removes the
session from ``_admin_sessions`` / ``_user_sessions``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import ADMIN_USER_ID, DEFAULT_ORG_ID
from mcpolis.domain.services import upstream_connection_service
from mcpolis.domain.services.oauth_liveness import (
    ProbeOutcome,
    _summarize_probe_outcomes,  # pyright: ignore[reportPrivateUsage]
    probe_upstream_liveness,
)
from tests.unit.factories import make_oauth_upstream


UPSTREAM_ID = "notion"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"


def _make_upstream(
    mode: AuthMode = AuthMode.admin_oauth,
) -> UpstreamDefinition:
    return make_oauth_upstream(
        id=UPSTREAM_ID, display_name="Notion", mode=mode, url=UPSTREAM_URL,
    )


def _make_session(
    list_tools_behavior: Any = None,
) -> MagicMock:
    """Build a session stand-in whose ``list_tools`` returns or
    raises per the behavior. An exception instance raises; anything
    else is returned."""
    session = MagicMock()

    async def _list_tools() -> Any:
        if isinstance(list_tools_behavior, BaseException):
            raise list_tools_behavior
        return list_tools_behavior or MagicMock()

    session.list_tools = _list_tools
    return session


def _install_client_manager_with_session(
    session: MagicMock, *, admin: bool = True,
    user_id: str = ADMIN_USER_ID,
) -> UpstreamClientManager:
    """Seed a real ``UpstreamClientManager`` via its public state-
    machine surface — ``iter_live_oauth_sessions`` and
    ``disconnect_user_session`` operate on the real storage, so
    seeding through the public API proves the probe reaches
    production bookkeeping rather than a mock shape that drifts."""
    from tests.unit._state_seed import seed_admin_session, seed_user_session

    cm = UpstreamClientManager(upstreams=[_make_upstream()])
    if admin:
        seed_admin_session(cm, UPSTREAM_ID, session=session)
    else:
        seed_user_session(cm, UPSTREAM_ID, user_id, session=session)
    return cm


@pytest.mark.asyncio
async def test_probe_healthy_session_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session whose ``list_tools`` returns normally must be left
    in place — no disconnect, no reconnect attempt. Returns
    ``ProbeOutcome.healthy`` so Gap C's loop can count it."""
    store = FileConnectionStore(tmp_path)
    session = _make_session(list_tools_behavior=MagicMock())
    cm = _install_client_manager_with_session(session)

    reconnect_spy = AsyncMock()
    monkeypatch.setattr(
        upstream_connection_service,
        "reconnect_with_stored_tokens", reconnect_spy,
    )

    outcome = await probe_upstream_liveness(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=ADMIN_USER_ID,
        session=session,
        client_manager=cm,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert outcome is ProbeOutcome.healthy
    # Session still in the admin dict.
    assert cm.is_connected(UPSTREAM_ID) is True
    reconnect_spy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        asyncio.TimeoutError(),
        TimeoutError(),
        httpx.ConnectError("net blip"),
        httpx.ConnectTimeout("slow"),
        OSError("transport"),
    ],
)
async def test_probe_transient_failures_leave_session_alone(
    exc: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brief network or transport hiccup during the probe must NOT
    tear down the session. A dying network during probe time is
    easily confused with a dead session; the cost of a false positive
    (wiping a perfectly good session, forcing user to re-auth) is
    much higher than the cost of waiting one more probe cycle."""
    store = FileConnectionStore(tmp_path)
    session = _make_session(list_tools_behavior=exc)
    cm = _install_client_manager_with_session(session)

    reconnect_spy = AsyncMock()
    monkeypatch.setattr(
        upstream_connection_service,
        "reconnect_with_stored_tokens", reconnect_spy,
    )

    outcome = await probe_upstream_liveness(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=ADMIN_USER_ID,
        session=session,
        client_manager=cm,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert outcome is ProbeOutcome.transient
    assert cm.is_connected(UPSTREAM_ID) is True
    reconnect_spy.assert_not_called()


@pytest.mark.asyncio
async def test_probe_fatal_error_tears_down_and_triggers_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-transient exception must (1) remove the session from
    the client manager and (2) call ``reconnect_with_stored_tokens``
    so §5.1's delete-vs-retry policy runs without the probe having
    to re-implement it."""
    store = FileConnectionStore(tmp_path)
    session = _make_session(
        list_tools_behavior=RuntimeError("auth dead"),
    )
    cm = _install_client_manager_with_session(session)

    reconnect_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(
        upstream_connection_service,
        "reconnect_with_stored_tokens", reconnect_spy,
    )

    upstream = _make_upstream()
    outcome = await probe_upstream_liveness(
        org_id=DEFAULT_ORG_ID,
        upstream=upstream,
        user_id=ADMIN_USER_ID,
        session=session,
        client_manager=cm,
        connection_store=store,
        server_url=SERVER_URL,
    )

    assert outcome is ProbeOutcome.torn_down
    # Admin session removed.
    assert cm.is_connected(UPSTREAM_ID) is False
    # Reconnect attempted with the same (org, upstream, user).
    reconnect_spy.assert_awaited_once()
    assert reconnect_spy.await_args is not None
    args, _kwargs = reconnect_spy.await_args
    # Positional args: (org, upstream, user_id, store, cm, server_url)
    assert args[0] == DEFAULT_ORG_ID
    assert args[1].id == UPSTREAM_ID
    assert args[2] == ADMIN_USER_ID


@pytest.mark.asyncio
async def test_probe_fatal_error_on_per_user_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-user sessions must tear down via the per-user path, not the
    admin path. Regression guard against a probe that ignores
    ``user_id`` and collapses every disconnect onto ``_close_admin``."""
    store = FileConnectionStore(tmp_path)
    session = _make_session(
        list_tools_behavior=RuntimeError("auth dead"),
    )
    cm = _install_client_manager_with_session(
        session, admin=False, user_id="alice@co.com",
    )

    reconnect_spy = AsyncMock(return_value=None)
    monkeypatch.setattr(
        upstream_connection_service,
        "reconnect_with_stored_tokens", reconnect_spy,
    )

    await probe_upstream_liveness(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
        session=session,
        client_manager=cm,
        connection_store=store,
        server_url=SERVER_URL,
    )

    # Per-user session removed, admin dict untouched.
    assert cm.has_user_session(UPSTREAM_ID, "alice@co.com") is False
    assert cm.has_admin_session(UPSTREAM_ID) is False
    reconnect_spy.assert_awaited_once()
    assert reconnect_spy.await_args is not None
    args, _ = reconnect_spy.await_args
    assert args[2] == "alice@co.com"


# ── iter_live_oauth_sessions shape ───────────────────────────────────


def test_iter_live_oauth_sessions_includes_admin_and_user_entries() -> None:
    """Sanity-check the manager-side API the probe depends on. Admin
    entries surface under ``ADMIN_USER_ID``; per-user entries surface
    under the real user_id. Service-account / non-OAuth upstreams
    are filtered out by the ``oauth_upstream_ids`` set so a
    service-account upstream's shared session never drags the probe
    into a meaningless ``list_tools`` request."""
    from tests.unit._state_seed import seed_admin_session, seed_user_session

    cm = UpstreamClientManager(upstreams=[])
    admin_sess = MagicMock()
    user_sess = MagicMock()
    other_sess = MagicMock()  # different upstream, should be filtered
    seed_admin_session(cm, UPSTREAM_ID, session=admin_sess)
    seed_user_session(
        cm, UPSTREAM_ID, "alice@co.com", session=user_sess,
    )
    seed_admin_session(cm, "slack", session=other_sess)

    entries = cm.iter_live_oauth_sessions({UPSTREAM_ID})

    ids = {(upstream_id, user_id) for upstream_id, user_id, _ in entries}
    assert (UPSTREAM_ID, ADMIN_USER_ID) in ids
    assert (UPSTREAM_ID, "alice@co.com") in ids
    # Slack (not in oauth_upstream_ids) must NOT be in the result.
    assert not any(u == "slack" for u, _, _ in entries)


# ── Gap C: _summarize_probe_outcomes aggregation ─────────────────────


def test_summarize_groups_outcomes_by_bucket() -> None:
    """The per-tick summary log reads its four counters off this
    function. A wrong bucket (e.g. grouping ``transient`` with
    ``healthy``) would silently mis-report the fleet state to
    operators — pin the branches explicitly."""
    results: list[ProbeOutcome | BaseException] = [
        ProbeOutcome.healthy,
        ProbeOutcome.healthy,
        ProbeOutcome.transient,
        ProbeOutcome.torn_down,
    ]
    counts = _summarize_probe_outcomes(results)
    assert counts == {
        "healthy": 2, "transient": 1, "torn_down": 1, "errors": 0,
    }


def test_summarize_counts_gather_exceptions_as_errors() -> None:
    """``asyncio.gather(..., return_exceptions=True)`` surfaces
    probe-internal bugs as exception instances in the result list.
    Those must fall into ``errors`` — not ``torn_down``, because an
    alert on "probe crashed" (operator should fix) should be
    distinguishable from "session died" (expected signal)."""
    results: list[ProbeOutcome | BaseException] = [
        ProbeOutcome.healthy,
        RuntimeError("probe blew up"),
        ProbeOutcome.torn_down,
    ]
    counts = _summarize_probe_outcomes(results)
    assert counts["errors"] == 1
    assert counts["torn_down"] == 1
    assert counts["healthy"] == 1
    assert counts["transient"] == 0
