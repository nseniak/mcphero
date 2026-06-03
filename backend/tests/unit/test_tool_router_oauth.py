"""Tests for ToolRouter OAuth-aware routing (admin_oauth and per_user_oauth)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
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
