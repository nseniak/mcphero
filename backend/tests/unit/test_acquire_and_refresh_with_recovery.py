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

from mcpolis.domain.model.upstream import DiscoveredTool
from mcpolis.domain.services.upstream_connection_service import (
    acquire_and_refresh_with_recovery,
)
from tests.unit.factories import make_upstream_definition


class _FakeManager:
    """Satisfies the slice of ``UpstreamClientManager`` the recovery
    wrapper + ``acquire_upstream_session`` touch for a service_account
    upstream."""

    def __init__(self) -> None:
        self.ensure_calls = 0
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        self.ensure_calls += 1

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return object()

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


class _FakeRegistry:
    """``refresh_upstream`` walks a scripted list of behaviours: an
    exception is raised, anything else is returned (the tool list)."""

    def __init__(self, behaviours: list[Any]) -> None:
        self._behaviours = behaviours
        self.calls = 0

    async def refresh_upstream(self, upstream_id: str) -> list[DiscoveredTool]:
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


async def _run(manager: _FakeManager, registry: _FakeRegistry, **kw: Any):
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
    manager = _FakeManager()
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    tools = await _run(manager, registry)

    assert len(tools) == 1
    assert registry.calls == 2, "must retry the refresh after a stall"
    assert manager.fresh_calls == 1, "must force a fresh reconnect before retry"


@pytest.mark.asyncio
async def test_non_stall_error_propagates_without_reconnect() -> None:
    manager = _FakeManager()
    registry = _FakeRegistry([RuntimeError("server said no")])

    with pytest.raises(RuntimeError, match="server said no"):
        await _run(manager, registry)

    assert registry.calls == 1, "a non-stall error must not be retried"
    assert manager.fresh_calls == 0, "no fresh reconnect for a non-stall error"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts() -> None:
    manager = _FakeManager()
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


class _FakeOAuthManager:
    """The slice of ``UpstreamClientManager`` that the recovery wrapper
    + ``acquire_upstream_session`` touch for an OAuth upstream: a
    per-user session cache with membership-only liveness, mirroring the
    real manager."""

    def __init__(self, user_id: str, upstream_id: str) -> None:
        self.sessions: dict[tuple[str, str], Any] = {
            (user_id, upstream_id): object(),
        }
        self.evictions: list[tuple[str, str]] = []

    def has_user_session(self, upstream_id: str, user_id: str) -> bool:
        return (user_id, upstream_id) in self.sessions

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        assert user_id is not None
        return self.sessions[(user_id, upstream_id)]

    async def disconnect_user_session(
        self, upstream_id: str, user_id: str
    ) -> None:
        self.evictions.append((user_id, upstream_id))
        self.sessions.pop((user_id, upstream_id), None)


async def _run_oauth(
    manager: _FakeOAuthManager,
    registry: _FakeRegistry,
    monkeypatch: pytest.MonkeyPatch,
    **kw: Any,
) -> tuple[Any, list[str]]:
    """Run the wrapper over a per_user_oauth upstream with
    ``reconnect_with_stored_tokens`` patched to act like a successful
    stored-token reconnect (installs a fresh session). Returns
    ``(result, effective_users reconnect was called with)``."""
    from mcpolis.domain.model.policy import AuthMode
    from mcpolis.domain.services import (
        upstream_connection_service as ucs_module,
    )
    from tests.unit.factories import make_upstream_auth

    upstream = make_upstream_definition(
        id="notion", auth=make_upstream_auth(mode=AuthMode.per_user_oauth),
    )
    reconnects: list[str] = []

    async def fake_reconnect(**kwargs: Any) -> None:
        effective_user = kwargs["effective_user"]
        reconnects.append(effective_user)
        manager.sessions[(effective_user, "notion")] = object()
        return None

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect
    )

    result = await acquire_and_refresh_with_recovery(
        org_id="acme",
        upstream=upstream,
        effective_user="alice@co.com",
        connection_store=cast(Any, object()),
        client_manager=cast(Any, manager),
        tool_registry=cast(Any, registry),
        server_url="http://localhost:8000",
        **kw,
    )
    return result, reconnects


@pytest.mark.asyncio
async def test_oauth_refresh_stall_evicts_session_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeOAuthManager("alice@co.com", "notion")
    registry = _FakeRegistry([asyncio.TimeoutError(), [make_tool()]])

    tools, reconnects = await _run_oauth(manager, registry, monkeypatch)

    assert len(tools) == 1
    assert registry.calls == 2, "must retry the refresh after a stall"
    assert manager.evictions == [("alice@co.com", "notion")], (
        "the stalled per-user session must be evicted before the retry"
    )
    assert reconnects == ["alice@co.com"], (
        "the retry must reconnect from stored tokens, not reuse the cache"
    )


@pytest.mark.asyncio
async def test_oauth_refresh_non_stall_error_does_not_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _FakeOAuthManager("alice@co.com", "notion")
    registry = _FakeRegistry([RuntimeError("server said no")])

    with pytest.raises(RuntimeError, match="server said no"):
        await _run_oauth(manager, registry, monkeypatch)

    assert manager.evictions == [], (
        "an ordinary refresh error must leave the cached session alone"
    )
