"""``acquire_and_refresh_with_recovery``: retry a refresh that hit a
transport stall by reconnecting on a FRESH session.

This is the recovery layer for E2B's intermittent post-reattach stdout
stall — refresh_upstream raises a transport stall, and the wrapper drops
the stalled session (``reconnect_shared_fresh``) and retries so the
operator gets a complete catalogue rather than a partial one.
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
    UpstreamStopped,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import DiscoveredTool, UpstreamDefinition
from mcpolis.domain.services import (
    upstream_connection_service as ucs_module,
)
from mcpolis.domain.services.upstream_connection_service import (
    UPSTREAM_STOPPED,
    SessionUnavailable,
    acquire_and_refresh_with_recovery,
)
from tests.unit._state_seed import seed_user_session
from tests.unit.factories import make_upstream_auth, make_upstream_definition
from tests.unit.stall_client_manager_fake import (
    StallClientManagerFake,
    make_stall_client_manager,
)


class _FakeRegistry:
    """``refresh_upstream`` walks a scripted list of behaviours: an
    exception is raised, anything else is returned (the tool list).
    Records the session each refresh was told to discover on."""

    def __init__(self, behaviours: list[Any]) -> None:
        self._behaviours = behaviours
        self.calls = 0
        self.sessions: list[Any] = []

    async def refresh_upstream(
        self, upstream_id: str, *, session: Any = None,
    ) -> list[DiscoveredTool]:
        self.sessions.append(session)
        b = self._behaviours[self.calls]
        self.calls += 1
        if isinstance(b, BaseException):
            raise b
        return cast(list[DiscoveredTool], b)


def make_tool() -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id="everything2",
        original_name="echo",
        prefixed_name="everything2__echo",
        description="echo",
        input_schema={},
    )


async def _run(
    manager: StallClientManagerFake, registry: _FakeRegistry, **kw: Any,
):
    upstream = make_upstream_definition(id="everything2")  # service_account
    return await acquire_and_refresh_with_recovery(
        org_id="acme",
        upstream=upstream,
        effective_user="",
        connection_store=None,
        client_manager=cast(Any, manager),
        tool_registry=cast(Any, registry),
        server_url="http://localhost:8000",
        **kw,
    )


@pytest.mark.asyncio
async def test_retries_on_transport_stall_then_succeeds() -> None:
    manager = make_stall_client_manager(session=object())
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    tools = await _run(manager, registry)

    assert len(tools) == 1
    assert registry.calls == 2, "must retry the refresh after a stall"
    assert manager.fresh_calls == 1, "must force a fresh reconnect before retry"
    assert registry.sessions == [manager.session, manager.session], (
        "each refresh must discover on the session acquired for it"
    )
    assert manager.stale_seen == [manager.session], (
        "the heal must be told which session stalled"
    )


@pytest.mark.asyncio
async def test_non_stall_error_propagates_without_reconnect() -> None:
    manager = make_stall_client_manager(session=object())
    registry = _FakeRegistry([RuntimeError("server said no")])

    with pytest.raises(RuntimeError, match="server said no"):
        await _run(manager, registry)

    assert registry.calls == 1, "a non-stall error must not be retried"
    assert manager.fresh_calls == 0, "no fresh reconnect for a non-stall error"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    manager = make_stall_client_manager(session=object())
    registry = _FakeRegistry([asyncio.TimeoutError(), asyncio.TimeoutError()])

    with pytest.raises(asyncio.TimeoutError):
        await _run(manager, registry, max_attempts=2)

    assert registry.calls == 2, "exactly max_attempts refreshes"
    assert manager.fresh_calls == 1, "one fresh reconnect between the two attempts"


# --- OAuth upstreams: a refresh stall must evict the cached per-user
# session, not just retry. ``acquire_upstream_session`` short-circuits
# to the cache on membership alone, so without eviction the retry
# refreshes over the same dead transport (same mechanism as the
# 2026-06-12 tool-call incident, surfacing here as a dashboard
# tool-refresh that can never recover until the idle sweep).


def make_oauth_manager(
    user_id: str, upstream_id: str,
) -> tuple[UpstreamClientManager, UpstreamDefinition, Any]:
    """A real manager holding one cached session for ``user_id`` on a
    per_user_oauth upstream. Returns ``(manager, upstream, cached)``."""
    upstream = make_upstream_definition(
        id=upstream_id, auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )
    manager = UpstreamClientManager([upstream])
    cached, _ = seed_user_session(manager, upstream_id, user_id)
    return manager, upstream, cached


async def _run_oauth(
    manager: UpstreamClientManager,
    upstream: UpstreamDefinition,
    registry: _FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
    **kw: Any,
) -> tuple[Any, list[str], list[Any]]:
    """Run the wrapper over a per_user_oauth upstream. One stored-token
    reconnect (the token dance and the connect) is stood in for: it
    installs a fresh session. Everything around it is real, so a cached
    session is reused and only a missing one reconnects. Returns
    ``(result, users a reconnect ran for, sessions it built)``."""
    reconnects: list[str] = []
    built: list[Any] = []

    async def fake_reconnect(**kwargs: Any) -> Any:
        effective_user = kwargs["effective_user"]
        reconnects.append(effective_user)
        session, _ = seed_user_session(manager, upstream.id, effective_user)
        built.append(session)
        return session

    monkeypatch.setattr(
        ucs_module, "_reconnect_from_stored_tokens", fake_reconnect,
    )

    result = await acquire_and_refresh_with_recovery(
        org_id="acme",
        upstream=upstream,
        effective_user="alice@co.com",
        connection_store=cast(Any, object()),
        client_manager=manager,
        tool_registry=cast(Any, registry),
        server_url="http://localhost:8000",
        **kw,
    )
    return result, reconnects, built


@pytest.mark.asyncio
async def test_oauth_refresh_stall_evicts_session_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, upstream, cached = make_oauth_manager("alice@co.com", "notion")
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    tools, reconnects, built = await _run_oauth(
        manager, upstream, registry, monkeypatch,
    )

    assert len(tools) == 1
    assert registry.calls == 2, "must retry the refresh after a stall"
    assert reconnects == ["alice@co.com"], (
        "the retry must reconnect from stored tokens, not reuse the cache"
    )
    assert registry.sessions == [cached, built[0]], (
        "the stalled per-user session must be evicted before the retry, "
        "and the retry must discover on the fresh one"
    )
    assert manager.find_user_session("notion", "alice@co.com") is built[0]


@pytest.mark.asyncio
async def test_oauth_refresh_non_stall_error_does_not_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, upstream, cached = make_oauth_manager("alice@co.com", "notion")
    registry = _FakeRegistry([RuntimeError("server said no")])

    with pytest.raises(RuntimeError, match="server said no"):
        await _run_oauth(manager, upstream, registry, monkeypatch)

    assert manager.find_user_session("notion", "alice@co.com") is cached, (
        "an ordinary refresh error must leave the cached session alone"
    )


@pytest.mark.asyncio
async def test_a_server_stopped_during_the_heal_reads_as_stopped() -> None:
    """The refresh stalls, and by the time it heals, an admin has stopped
    the server. That is a refusal the admin asked for: the caller gets
    "upstream stopped", which callers log quietly, not the raw refusal.
    (Review of the follow-up fixes, F5.)"""
    manager = make_stall_client_manager(
        session=object(),
        heal_error=UpstreamStopped("upstream 'mee6' is stopped"),
    )
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    with pytest.raises(SessionUnavailable) as refused:
        await _run(manager, registry)

    assert refused.value.reason == UPSTREAM_STOPPED
