"""Tests for ToolRouter OAuth-aware routing (admin_oauth and per_user_oauth)."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import mcp.types as mcp_types
import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.upstream import ToolAnnotations
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services import (
    upstream_connection_service as ucs_module,
)
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (
    DisconnectReason,
)


def make_test_policy_engine(
    admin_emails: list[str] | None = None,
) -> PolicyEngine:
    """Build a PolicyEngine with the given admin emails registered.

    Phase 2's admin_oauth lookup enumerates admin emails from the
    policy engine, so admin_oauth tests must seed at least one admin
    user (otherwise the pool is empty and the router returns
    admin_unavailable_error). Defaulting to no admins keeps
    per_user_oauth / service_account tests cheap.
    """
    from mcpolis.domain.model.settings import RoleDefinition, UserDefinition

    if not admin_emails:
        return PolicyEngine(SettingsConfig())
    config = SettingsConfig(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={
            email: UserDefinition(role="admin")
            for email in admin_emails
        },
    )
    return PolicyEngine(config)
from tests.unit.factories import make_discovered_tool, make_upstream_definition


def make_upstream_auth_oauth(
    mode: AuthMode = AuthMode.admin_oauth,
) -> UpstreamAuthConfig:
    return UpstreamAuthConfig(
        mode=mode,
        scopes=["read"],
    )


def make_mock_session() -> AsyncMock:
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")],
            isError=False,
        )
    )
    return mock_session


def make_oauth_tool_router(
    tmp_path: Path,
    upstream_id: str = "slack",
    auth_mode: AuthMode = AuthMode.admin_oauth,
    admin_emails: list[str] | None = None,
    annotations: ToolAnnotations | None = None,
) -> tuple[ToolRouter, UpstreamClientManager, ConnectionStore]:
    auth = make_upstream_auth_oauth(mode=auth_mode)
    upstream = make_upstream_definition(id=upstream_id, auth=auth)

    client_manager = UpstreamClientManager([upstream])
    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [
        make_discovered_tool(
            upstream_id=upstream_id,
            original_name="send_message",
            annotations=annotations,
        ),
    ]

    connection_store = AsyncMock(spec=ConnectionStore)
    connection_store.get_user_token.return_value = None
    connection_store.get_client_info.return_value = None

    # admin_oauth pool resolution iterates ``policy_engine.get_admin_emails()``
    # at call time. Default the admin set to ``["admin@co.com"]`` for
    # admin_oauth tests so the pool path is exercisable; per-test
    # callers can override.
    if admin_emails is None and auth_mode == AuthMode.admin_oauth:
        admin_emails = ["admin@co.com"]

    router = ToolRouter(
        registry,
        client_manager,
        audit_service,
        [upstream],
        policy_engine=make_test_policy_engine(admin_emails=admin_emails),
        connection_store=connection_store,
        server_url="http://localhost:8000",
    )
    return router, client_manager, connection_store


# --- service_account tests (unchanged behavior) ---


@pytest.mark.asyncio
async def test_service_account_uses_shared_session(
    tmp_path: Path,
) -> None:
    auth = UpstreamAuthConfig(mode=AuthMode.service_account)
    upstream = make_upstream_definition(id="github", auth=auth)
    client_manager = UpstreamClientManager([upstream])
    mock_session = make_mock_session()
    from tests.unit._state_seed import seed_shared_session
    seed_shared_session(client_manager, "github", session=mock_session)

    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [
        make_discovered_tool(
            upstream_id="github", original_name="create_issue"
        ),
    ]
    router = ToolRouter(
        registry, client_manager, audit_service, [upstream],
        policy_engine=make_test_policy_engine(),
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="github__create_issue",
        arguments={"title": "Bug"},
        user_id="alice",
        session_id=None,
    )

    assert not result.isError
    mock_session.call_tool.assert_awaited_once()


# --- OAuth: existing session reused ---


@pytest.mark.asyncio
async def test_oauth_reuses_existing_user_session(
    tmp_path: Path,
) -> None:
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth
    )

    # Pre-populate per-user session
    mock_session = make_mock_session()
    key = ("alice@co.com", "slack")
    client_manager._user_sessions[key] = mock_session
    client_manager._user_session_last_used[key] = 0.0

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={"text": "hello"},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError
    mock_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_oauth_reuses_existing_admin_pool_session(
    tmp_path: Path,
) -> None:
    """An admin's live per-user session is reused when serving
    admin_oauth traffic — even from a non-admin caller — without
    triggering a reconnect.
    """
    router, client_manager, connection_store = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["admin@co.com"],
    )

    # The admin's token is in the connection store (so the pool
    # resolver picks them) and their session is already live.
    from datetime import UTC, datetime

    async def _resolve_token(
        org: str, user: str, upstream: str,
    ) -> InternalOAuthToken | None:
        if user == "admin@co.com":
            return InternalOAuthToken(
                access_token="x",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                refresh_token_created_at=datetime.now(UTC),
            )
        return None

    connection_store.get_user_token.side_effect = _resolve_token  # type: ignore[attr-defined]

    mock_session = make_mock_session()
    client_manager._user_sessions[("admin@co.com", "slack")] = mock_session
    client_manager._user_session_last_used[("admin@co.com", "slack")] = 0.0

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",  # non-admin caller
        session_id=None,
    )

    assert not result.isError
    mock_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_oauth_no_admin_connected_returns_unavailable(
    tmp_path: Path,
) -> None:
    """When no admin is in the pool and no legacy admin token exists,
    the router surfaces the admin_unavailable_error instead of
    silently failing."""
    router, _, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["admin@co.com"],
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert "not currently available" in text


@pytest.mark.asyncio
async def test_admin_oauth_picks_most_recently_refreshed_admin(
    tmp_path: Path,
) -> None:
    """When several admins have stored tokens, the pool returns the
    one whose token was refreshed most recently — so a freshly
    reconnected admin starts handling traffic right away."""
    from datetime import UTC, datetime, timedelta

    router, client_manager, connection_store = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["alice@co.com", "bob@co.com"],
    )

    now = datetime.now(UTC)
    tokens = {
        "alice@co.com": InternalOAuthToken(
            access_token="alice",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            refresh_token_created_at=now - timedelta(hours=1),
        ),
        "bob@co.com": InternalOAuthToken(
            access_token="bob",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            refresh_token_created_at=now,  # newer
        ),
    }

    async def _resolve_token(
        org: str, user: str, upstream: str,
    ) -> InternalOAuthToken | None:
        return tokens.get(user)

    connection_store.get_user_token.side_effect = _resolve_token  # type: ignore[attr-defined]

    bob_session = make_mock_session()
    client_manager._user_sessions[("bob@co.com", "slack")] = bob_session
    client_manager._user_session_last_used[("bob@co.com", "slack")] = 0.0
    alice_session = make_mock_session()
    client_manager._user_sessions[("alice@co.com", "slack")] = alice_session
    client_manager._user_session_last_used[("alice@co.com", "slack")] = 0.0

    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    bob_session.call_tool.assert_awaited_once()
    alice_session.call_tool.assert_not_called()


# --- OAuth not configured ---


@pytest.mark.asyncio
async def test_oauth_not_configured_returns_error(
    tmp_path: Path,
) -> None:
    auth = make_upstream_auth_oauth(mode=AuthMode.admin_oauth)
    upstream = make_upstream_definition(id="slack", auth=auth)
    client_manager = UpstreamClientManager([upstream])
    audit_service = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], client_manager)
    registry._tools = [
        make_discovered_tool(
            upstream_id="slack", original_name="send_message"
        ),
    ]

    router = ToolRouter(
        registry,
        client_manager,
        audit_service,
        [upstream],
        policy_engine=make_test_policy_engine(),
        # No connection_store or auth_coordinator
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert "OAuth is not configured" in text


# --- Audit logging ---


@pytest.mark.asyncio
async def test_audit_logs_oauth_auth_identity(
    tmp_path: Path,
) -> None:
    """Audit identity for admin_oauth carries the chosen pool admin's
    email (Phase 2). Pre-Phase-2 it carried the synthetic ``__admin__``
    sentinel; that audit shape changes deliberately so analytics can
    answer "which admin's credentials served this call?"."""
    from datetime import UTC, datetime

    router, client_manager, connection_store = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["admin@co.com"],
    )

    async def _resolve_token(
        org: str, user: str, upstream: str,
    ) -> InternalOAuthToken | None:
        if user == "admin@co.com":
            return InternalOAuthToken(
                access_token="x",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                refresh_token_created_at=datetime.now(UTC),
            )
        return None

    connection_store.get_user_token.side_effect = _resolve_token  # type: ignore[attr-defined]

    mock_session = make_mock_session()
    client_manager._user_sessions[("admin@co.com", "slack")] = mock_session
    client_manager._user_session_last_used[("admin@co.com", "slack")] = 0.0

    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    audit_path = tmp_path / "audit.jsonl"
    entry = json.loads(audit_path.read_text().strip())
    assert entry["auth_mode"] == "admin_oauth"
    assert entry["auth_identity"] == "admin_oauth:slack:admin@co.com"


# --- OAuth: reconnect from stored tokens ---
#
# Regression: a previous refactor inverted the success/failure branch
# after `reconnect_with_stored_tokens` started returning
# `DisconnectReason | None` (None == success). On idle-disconnected
# per-user sessions with valid stored tokens, the router would tell
# the user "you are not signed in" instead of reconnecting.


@pytest.mark.asyncio
async def test_per_user_oauth_reconnects_from_stored_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth
    )

    # No live session — simulates an idle-disconnected user.
    assert not client_manager.has_user_session("slack", "alice@co.com")

    mock_session = make_mock_session()

    async def fake_reconnect(**kwargs: object) -> DisconnectReason | None:
        # Simulate a successful reconnect: populate the per-user
        # session the way the real implementation does.
        key = ("alice@co.com", "slack")
        client_manager._user_sessions[key] = mock_session
        client_manager._user_session_last_used[key] = 0.0
        return None

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={"text": "hi"},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError
    mock_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_per_user_oauth_no_stored_tokens_returns_signin_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth
    )

    async def fake_reconnect(**kwargs: object) -> DisconnectReason | None:
        return DisconnectReason.no_tokens

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert "not signed in" in text
    assert "/my-tools" in text


# --- effective_user substitution: the load-bearing routing rule ---
#
# Phase 2: the router substitutes ``effective_user=<chosen pool admin's
# email>`` for ``admin_oauth`` and ``effective_user=user_id`` for
# ``per_user_oauth``. That branch keeps non-admin callers off any
# admin's per-user tokens while letting admin_oauth upstreams stay
# operational as long as any admin in the org has a valid token. Pin
# it by inspecting what the router actually passes downstream, not
# just the end state.


@pytest.mark.asyncio
async def test_admin_oauth_passes_pool_admin_email_regardless_of_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For ``admin_oauth``, every non-admin caller must resolve to the
    chosen pool admin's email in the session lookup and reconnect
    call. A regression would silently re-key the lookup to the
    caller's own email and fail to find the admin's tokens."""
    from datetime import UTC, datetime

    router, client_manager, connection_store = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["admin@co.com"],
    )

    async def _resolve_token(
        org: str, user: str, upstream: str,
    ) -> InternalOAuthToken | None:
        if user == "admin@co.com":
            return InternalOAuthToken(
                access_token="x",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                refresh_token_created_at=datetime.now(UTC),
            )
        return None

    connection_store.get_user_token.side_effect = _resolve_token  # type: ignore[attr-defined]

    seen: dict[str, object] = {}

    async def capture_reconnect(**kwargs: object) -> DisconnectReason | None:
        seen.update(kwargs)
        # Populate the admin's per-user session so the router reaches
        # its happy-path return branch.
        client_manager._user_sessions[("admin@co.com", "slack")] = (
            make_mock_session()
        )
        client_manager._user_session_last_used[("admin@co.com", "slack")] = 0.0
        return None

    monkeypatch.setattr(
        ucs_module,
        "reconnect_with_stored_tokens",
        capture_reconnect,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError
    assert seen["effective_user"] == "admin@co.com"
    assert seen["upstream"].id == "slack"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_per_user_oauth_passes_callers_user_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a ``per_user_oauth`` upstream, the router must pass the
    caller's own ``user_id`` through — not the ``__admin__`` sentinel.
    A regression here would either silently ride on admin tokens
    (privacy/scope leak) or cross-read another user's tokens if the
    lookup key got scrambled."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth
    )

    seen: dict[str, object] = {}

    async def capture_reconnect(**kwargs: object) -> DisconnectReason | None:
        seen.update(kwargs)
        key = ("alice@co.com", "slack")
        client_manager._user_sessions[key] = make_mock_session()
        client_manager._user_session_last_used[key] = 0.0
        return None

    monkeypatch.setattr(
        ucs_module,
        "reconnect_with_stored_tokens",
        capture_reconnect,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError
    assert seen["effective_user"] == "alice@co.com"


@pytest.mark.asyncio
async def test_per_user_oauth_two_users_each_use_their_own_session(
    tmp_path: Path,
) -> None:
    """Two users calling the same per_user_oauth upstream must each
    hit their own pre-existing session. If the router ever collapsed
    both to a single key (e.g. by short-circuiting through
    ``__admin__``), alice's tool call would land on bob's session and
    the audit trail would be ambiguous."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth
    )

    alice_session = make_mock_session()
    bob_session = make_mock_session()
    client_manager._user_sessions[("alice@co.com", "slack")] = alice_session
    client_manager._user_session_last_used[("alice@co.com", "slack")] = 0.0
    client_manager._user_sessions[("bob@co.com", "slack")] = bob_session
    client_manager._user_session_last_used[("bob@co.com", "slack")] = 0.0

    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={"text": "from alice"},
        user_id="alice@co.com",
        session_id=None,
    )
    await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={"text": "from bob"},
        user_id="bob@co.com",
        session_id=None,
    )

    alice_session.call_tool.assert_awaited_once()
    bob_session.call_tool.assert_awaited_once()
    # Cross-checks: neither session received the other user's payload.
    # ``call_tool(name, arguments)`` is invoked positionally by the router.
    alice_args = alice_session.call_tool.await_args
    bob_args = bob_session.call_tool.await_args
    assert alice_args.args[1] == {"text": "from alice"}  # type: ignore[union-attr]
    assert bob_args.args[1] == {"text": "from bob"}  # type: ignore[union-attr]


# --- OAuth: transport stall recovery ---
#
# Prod incident 2026-06-12 (Sentry MCPOLIS-BACKEND-R/-S): a per-user
# OAuth session over streamable HTTP died during a 14-minute idle gap
# (server closed the connection; the SDK task group exited and closed
# the in-memory streams), but the dead ClientSession stayed cached.
# ``acquire_upstream_session`` short-circuits to the cache whenever an
# entry exists — membership only, no liveness — so the stall retry got
# the SAME dead session back and every call failed with
# ``anyio.ClosedResourceError`` until the idle sweep evicted it 31
# minutes later. The recovery contract pinned here: a transport stall
# on an OAuth session evicts it, so the retry (or, for non-idempotent
# tools, the next call) reconnects from stored tokens.


def make_dead_session() -> AsyncMock:
    """A cached session whose transport is gone — ``call_tool`` raises
    the exact exception the prod incident produced."""
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=anyio.ClosedResourceError())
    return session


def make_real_dead_client_session() -> ClientSession:
    """A REAL ``mcp.ClientSession`` over genuinely closed anyio memory
    streams — not a mock raising the exception we assume. ``call_tool``
    must die inside ``send_request``'s write exactly like prod
    (``anyio.streams.memory.send_nowait`` → ``ClosedResourceError``)."""
    client_to_server_send, _ = anyio.create_memory_object_stream[Any](10)
    _, server_to_client_recv = anyio.create_memory_object_stream[Any](10)
    session = ClientSession(server_to_client_recv, client_to_server_send)
    client_to_server_send.close()
    return session


def install_session(
    client_manager: UpstreamClientManager,
    user_id: str,
    upstream_id: str,
    session: Any,
) -> None:
    key = (user_id, upstream_id)
    client_manager._user_sessions[key] = session
    client_manager._user_session_last_used[key] = 0.0


def install_fake_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    client_manager: UpstreamClientManager,
    upstream_id: str,
    session_factory: Callable[[], Any],
) -> list[str]:
    """Patch ``reconnect_with_stored_tokens`` to act like a successful
    stored-token reconnect: install a fresh session for the effective
    user. Returns the list of effective users it was called with."""
    reconnects: list[str] = []

    async def fake_reconnect(**kwargs: Any) -> DisconnectReason | None:
        effective_user = kwargs["effective_user"]
        reconnects.append(effective_user)
        install_session(
            client_manager, effective_user, upstream_id, session_factory(),
        )
        return None

    monkeypatch.setattr(
        ucs_module, "reconnect_with_stored_tokens", fake_reconnect
    )
    return reconnects


@pytest.mark.asyncio
async def test_per_user_oauth_stall_evicts_dead_session_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod incident, replayed: dead cached session + idempotent
    tool. The stall must evict the dead session so the in-call retry
    reconnects from stored tokens and returns the real result — not the
    opaque error antoine got."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
        annotations=ToolAnnotations(idempotentHint=True),
    )
    install_session(client_manager, "alice@co.com", "slack", make_dead_session())
    fresh_session = make_mock_session()
    reconnects = install_fake_reconnect(
        monkeypatch, client_manager, "slack", lambda: fresh_session,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={"text": "hi"},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError, "retry must land on a fresh transport"
    assert reconnects == ["alice@co.com"]
    fresh_session.call_tool.assert_awaited_once_with(
        "send_message", {"text": "hi"}
    )


@pytest.mark.asyncio
async def test_per_user_oauth_stall_recovery_with_real_closed_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above but the dead session is a REAL ClientSession over
    closed anyio memory streams — pins recovery against the genuine
    SDK/anyio failure path rather than a mocked exception."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    install_session(
        client_manager, "alice@co.com", "slack",
        make_real_dead_client_session(),
    )
    fresh_session = make_mock_session()
    reconnects = install_fake_reconnect(
        monkeypatch, client_manager, "slack", lambda: fresh_session,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert not result.isError
    assert reconnects == ["alice@co.com"]
    fresh_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_per_user_oauth_non_idempotent_stall_heals_for_next_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-idempotent tool must NOT be retried in-call (it may have
    side effects) — the caller gets the opaque error — but the dead
    session must still be evicted so the user's NEXT call reconnects
    and succeeds instead of inheriting the poisoned session for the
    rest of the idle-sweep window."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
        annotations=None,
    )
    dead_session = make_dead_session()
    install_session(client_manager, "alice@co.com", "slack", dead_session)
    fresh_session = make_mock_session()
    reconnects = install_fake_reconnect(
        monkeypatch, client_manager, "slack", lambda: fresh_session,
    )

    first = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )
    assert first.isError, "non-idempotent stall is not retried in-call"
    assert dead_session.call_tool.await_count == 1
    assert not client_manager.has_user_session("slack", "alice@co.com") or (
        client_manager._user_sessions[("alice@co.com", "slack")]
        is not dead_session
    ), "the dead session must be evicted on stall"

    second = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )
    assert not second.isError, "the next call must recover via reconnect"
    assert reconnects == ["alice@co.com"]
    fresh_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_oauth_stall_evicts_owner_session_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admin_oauth: the session that stalls belongs to the pool owner,
    not the caller — eviction + reconnect must be keyed by the owner's
    email so a non-admin caller's retry rides the restored owner
    session."""
    from datetime import UTC, datetime

    router, client_manager, connection_store = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
        admin_emails=["admin@co.com"],
        annotations=ToolAnnotations(idempotentHint=True),
    )

    async def _resolve_token(
        org: str, user: str, upstream: str,
    ) -> InternalOAuthToken | None:
        if user == "admin@co.com":
            return InternalOAuthToken(
                access_token="x",
                refresh_token=None,
                expires_at=None,
                scopes=[],
                refresh_token_created_at=datetime.now(UTC),
            )
        return None

    connection_store.get_user_token.side_effect = _resolve_token  # type: ignore[attr-defined]

    install_session(client_manager, "admin@co.com", "slack", make_dead_session())
    fresh_session = make_mock_session()
    reconnects = install_fake_reconnect(
        monkeypatch, client_manager, "slack", lambda: fresh_session,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",  # non-admin caller
        session_id=None,
    )

    assert not result.isError
    assert reconnects == ["admin@co.com"], (
        "eviction + reconnect must target the pool owner's session"
    )
    fresh_session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_per_user_oauth_ordinary_error_does_not_evict(
    tmp_path: Path,
) -> None:
    """A normal server-side error (the transport answered — it is
    fine) must NOT evict the session: evicting on every error would
    turn each upstream hiccup into a reconnect storm."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
        annotations=ToolAnnotations(idempotentHint=True),
    )
    session = make_mock_session()
    session.call_tool = AsyncMock(side_effect=RuntimeError("server said no"))
    install_session(client_manager, "alice@co.com", "slack", session)

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert result.isError
    assert (
        client_manager._user_sessions[("alice@co.com", "slack")] is session
    ), "an ordinary error must leave the cached session alone"
    assert session.call_tool.await_count == 1, (
        "an ordinary error must not be retried"
    )


@pytest.mark.asyncio
async def test_per_user_oauth_stall_on_both_attempts_returns_opaque_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the reconnected session stalls too, the caller gets the
    opaque correlation-id error (never a raw exception), and the
    second dead session is evicted as well so a later call starts
    clean."""
    router, client_manager, _ = make_oauth_tool_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
        annotations=ToolAnnotations(idempotentHint=True),
    )
    install_session(client_manager, "alice@co.com", "slack", make_dead_session())
    reconnects = install_fake_reconnect(
        monkeypatch, client_manager, "slack", make_dead_session,
    )

    result = await router.route_call(
        org_id=DEFAULT_ORG_ID,
        prefixed_name="slack__send_message",
        arguments={},
        user_id="alice@co.com",
        session_id=None,
    )

    assert result.isError
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Upstream tool call failed" in text
    assert "Reference:" in text
    assert reconnects == ["alice@co.com"], "exactly one in-call retry"
    assert not client_manager.has_user_session("slack", "alice@co.com"), (
        "the second dead session must be evicted too"
    )
