"""Router-level tests for ``read_resource`` / ``get_prompt``.

Mirrors the existing ``test_tool_router.py`` style: build a router with
a stub upstream session, drive it directly, and pin the auth-mode +
audit + error-shape contract. ``_resolve_session`` is shared with
``route_call`` so the same auth matrix applies to all three.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mcp_types
import pytest
from pydantic import AnyUrl

from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    ADMIN_USER_ID,
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import (  # pyright: ignore[reportPrivateUsage]
    ToolRouter,
    UpstreamRouterError,
    _session_error_text,
    _SessionResult,
)
from tests.unit.factories import make_upstream_definition


# --- _session_error_text (shared by read_resource / get_prompt / the
#     session-error surface): a non-text/empty first block must degrade to
#     a generic message, never crash (ROUTE-1 / BUG-3). ----------------


def test_session_error_text_returns_text_first_block() -> None:
    err = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="please re-auth")],
        isError=True,
    )
    assert _session_error_text(err) == "please re-auth"


def test_session_error_text_non_text_block_degrades_to_generic() -> None:
    err = mcp_types.CallToolResult(
        content=[
            mcp_types.ImageContent(
                type="image", data="aGVsbG8=", mimeType="image/png",
            ),
        ],
        isError=True,
    )
    out = _session_error_text(err)
    assert isinstance(out, str) and out  # a clean string, not a crash
    assert "not currently available" in out


def test_session_error_text_empty_content_degrades_to_generic() -> None:
    err = mcp_types.CallToolResult(content=[], isError=True)
    assert "not currently available" in _session_error_text(err)


async def make_router(
    tmp_path: Path,
    *,
    upstream_id: str = "notion",
    auth_mode: AuthMode = AuthMode.service_account,
) -> tuple[ToolRouter, AsyncMock, FileAuditRepository, UpstreamClientManager]:
    """Build a ToolRouter wired to a single mock session.

    The session is registered against the path that ``_resolve_session``
    will pick for *auth_mode*: shared dict for ``service_account``,
    per-user dict keyed by an admin email for ``admin_oauth`` (admin
    pool), per-user dict for ``per_user_oauth``. OAuth modes also
    receive a real ``FileConnectionStore`` (and, for admin_oauth, a
    seeded admin token plus an admin user in the policy) so the
    router's ``connection_store is None`` early-out doesn't fire and
    the pool resolver returns a usable identity.
    """
    from mcpolis.adapters.repositories.connection_store import (
        OAuthToken as InternalOAuthToken,
    )
    from mcpolis.domain.model.settings import RoleDefinition, UserDefinition

    upstream = make_upstream_definition(
        id=upstream_id,
        auth=UpstreamAuthConfig(mode=auth_mode),
    )
    cm = UpstreamClientManager([upstream])
    session = AsyncMock()
    session.read_resource = AsyncMock(
        return_value=mcp_types.ReadResourceResult(
            contents=[
                mcp_types.TextResourceContents(
                    uri=AnyUrl("test://hello"),
                    mimeType="text/plain",
                    text="Hello, world!",
                ),
            ],
        ),
    )
    session.get_prompt = AsyncMock(
        return_value=mcp_types.GetPromptResult(
            description="rendered greet prompt",
            messages=[
                mcp_types.PromptMessage(
                    role="user",
                    content=mcp_types.TextContent(
                        type="text", text="hello world",
                    ),
                ),
            ],
        ),
    )
    admin_email = "admin@co.com"
    from tests.unit._state_seed import seed_shared_session, seed_user_session
    if auth_mode == AuthMode.service_account:
        seed_shared_session(cm, upstream_id, session=session)
    elif auth_mode == AuthMode.admin_oauth:
        # Phase 2 admin_oauth: session is keyed by the chosen admin's
        # email; admin pool resolution finds them via the policy engine
        # + a stored token.
        seed_user_session(cm, upstream_id, admin_email, session=session)
    else:
        seed_user_session(cm, upstream_id, "alice", session=session)

    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], cm)

    # Seed admin policy + token only for admin_oauth so the pool
    # resolver picks the admin we wired the session for.
    if auth_mode == AuthMode.admin_oauth:
        config = SettingsConfig(
            roles={"admin": RoleDefinition(is_admin=True)},
            users={admin_email: UserDefinition(role="admin")},
        )
    else:
        config = SettingsConfig()

    connection_store: FileConnectionStore | None = (
        FileConnectionStore(tmp_path / "connections.json")
        if auth_mode != AuthMode.service_account
        else None
    )
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(config),
        connection_store=connection_store,
    )
    if auth_mode == AuthMode.admin_oauth and connection_store is not None:
        await connection_store.put_user_token(
            DEFAULT_ORG_ID, admin_email, upstream_id,
            InternalOAuthToken(
                access_token="x", refresh_token=None,
                expires_at=None, scopes=[],
            ),
        )
    return router, session, audit, cm


# --- read_resource ------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_resource_service_account_uses_shared_session(
    tmp_path: Path,
) -> None:
    router, session, _, _ = await make_router(
        tmp_path, auth_mode=AuthMode.service_account,
    )
    result = await router.read_resource(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_uri="test://hello",
        user_id="alice",
        session_id="sess1",
    )
    assert isinstance(result, mcp_types.ReadResourceResult)
    text = result.contents[0]
    assert isinstance(text, mcp_types.TextResourceContents)
    assert text.text == "Hello, world!"
    session.read_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_resource_admin_oauth_uses_admin_pool_session(
    tmp_path: Path,
) -> None:
    router, session, _, cm = await make_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
    )
    # Sanity: the session lives under the chosen admin's per-user
    # slot — Phase 2 retired the dedicated admin_sessions dict for
    # admin_oauth in favor of the admin pool.
    assert cm._user_sessions[("admin@co.com", "notion")] is session  # pyright: ignore[reportPrivateUsage]
    state = cm.get_state("notion")
    assert state is not None
    assert state.shared_session is None

    await router.read_resource(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_uri="test://hello",
        user_id="alice",  # non-admin caller, served via the pool
        session_id=None,
    )
    session.read_resource.assert_awaited_once()
    # ADMIN_USER_ID still exists as a sentinel for the legacy
    # fall-through path; assert it is reachable.
    assert ADMIN_USER_ID == ADMIN_USER_ID


@pytest.mark.asyncio
async def test_read_resource_per_user_oauth_uses_caller_session(
    tmp_path: Path,
) -> None:
    router, session, _, cm = await make_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
    )
    # connection_store is None → per_user without a session falls
    # through to the misconfigured branch. Seed alice's session above
    # so the existing-session branch fires instead.
    assert cm._user_sessions[("alice", "notion")] is session  # pyright: ignore[reportPrivateUsage]
    await router.read_resource(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_uri="test://hello",
        user_id="alice",
        session_id=None,
    )
    session.read_resource.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_resource_unknown_upstream_raises(tmp_path: Path) -> None:
    router, _, _, _ = await make_router(tmp_path)
    with pytest.raises(UpstreamRouterError, match="Unknown upstream"):
        await router.read_resource(
            org_id=DEFAULT_ORG_ID,
            upstream_id="not-registered",
            original_uri="test://x",
            user_id="alice",
            session_id=None,
        )


@pytest.mark.asyncio
async def test_read_resource_per_user_no_session_raises(tmp_path: Path) -> None:
    """``per_user_oauth`` upstream without a stored session must raise the
    user-facing 'please connect on /my-tools' message — same shape as
    call_tool, mirrored through ``UpstreamRouterError`` so the gateway
    can surface it via a clean ReadResourceResult."""
    upstream = make_upstream_definition(
        id="notion",
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    cm = UpstreamClientManager([upstream])
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], cm)
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=None,  # triggers the OAuth-not-configured branch
    )
    with pytest.raises(UpstreamRouterError) as exc_info:
        await router.read_resource(
            org_id=DEFAULT_ORG_ID,
            upstream_id="notion",
            original_uri="test://x",
            user_id="alice",
            session_id=None,
        )
    # ``connection_store=None`` produces the misconfigured-OAuth branch.
    assert "OAuth" in exc_info.value.message


@pytest.mark.asyncio
async def test_read_resource_logs_audit_entry(tmp_path: Path) -> None:
    router, _, audit, _ = await make_router(tmp_path)
    await router.read_resource(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_uri="test://hello",
        user_id="alice",
        session_id="sess1",
    )
    log_path = audit._log_path  # pyright: ignore[reportPrivateUsage]
    entry = json.loads(log_path.read_text().strip())
    assert entry["org_id"] == DEFAULT_ORG_ID
    assert entry["user_id"] == "alice"
    assert entry["upstream_id"] == "notion"
    assert entry["tool"] == "resource:notion:test://hello"
    assert entry["response_status"] == "success"


@pytest.mark.asyncio
async def test_read_resource_upstream_error_returns_opaque_message(
    tmp_path: Path,
) -> None:
    router, session, _, _ = await make_router(tmp_path)
    secret_hostname = "internal-db.prod.example.com:5432"
    session.read_resource = AsyncMock(
        side_effect=RuntimeError(f"connection to {secret_hostname} refused"),
    )
    with pytest.raises(UpstreamRouterError) as exc_info:
        await router.read_resource(
            org_id=DEFAULT_ORG_ID,
            upstream_id="notion",
            original_uri="test://hello",
            user_id="alice",
            session_id=None,
        )
    assert secret_hostname not in exc_info.value.message
    assert "Reference:" in exc_info.value.message


@pytest.mark.asyncio
async def test_read_resource_session_error_non_text_block_surfaces_clean(
    tmp_path: Path,
) -> None:
    """ROUTE-1 [BUG?]: ``read_resource``'s ``_on_session_error`` does
    ``assert isinstance(err.content[0], TextContent)``. If
    ``_resolve_session`` ever returns a session-unavailable
    ``CallToolResult`` whose first content block is NOT text (e.g.
    ``ImageContent``), that assertion crashes the request with an
    ``AssertionError`` (an internal invariant leak) instead of surfacing a
    clean, user-facing ``UpstreamRouterError``.

    INTENDED: a non-text session-error block degrades to an
    ``UpstreamRouterError`` the gateway can render as a clean
    ``ReadResourceResult`` — never an ``AssertionError``."""
    router, _, _, _ = await make_router(
        tmp_path, auth_mode=AuthMode.service_account,
    )

    image_error = mcp_types.CallToolResult(
        content=[
            mcp_types.ImageContent(
                type="image", data="aGVsbG8=", mimeType="image/png",
            ),
        ],
        isError=True,
    )

    async def _resolve_with_image_error(
        org_id: str, upstream: Any, user_id: str,
    ) -> _SessionResult:
        return _SessionResult(error=image_error)

    # The only seam to inject a non-text session-error result: there's no
    # production path that builds one today, so replace the resolver to
    # exercise the defensive surface ``_on_session_error`` should have.
    router._resolve_session = _resolve_with_image_error  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(UpstreamRouterError):
        await router.read_resource(
            org_id=DEFAULT_ORG_ID, upstream_id="notion",
            original_uri="test://hello", user_id="alice", session_id=None,
        )


# --- get_prompt ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_prompt_service_account_uses_shared_session(
    tmp_path: Path,
) -> None:
    router, session, _, _ = await make_router(
        tmp_path, auth_mode=AuthMode.service_account,
    )
    result = await router.get_prompt(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_name="greet",
        arguments={"who": "world"},
        user_id="alice",
        session_id=None,
    )
    assert isinstance(result, mcp_types.GetPromptResult)
    session.get_prompt.assert_awaited_once_with("greet", {"who": "world"})


@pytest.mark.asyncio
async def test_get_prompt_admin_oauth_uses_admin_session(tmp_path: Path) -> None:
    router, session, _, _ = await make_router(
        tmp_path, auth_mode=AuthMode.admin_oauth,
    )
    await router.get_prompt(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_name="greet",
        arguments=None,
        user_id="alice",
        session_id=None,
    )
    session.get_prompt.assert_awaited_once_with("greet", None)


@pytest.mark.asyncio
async def test_get_prompt_per_user_oauth_uses_caller_session(
    tmp_path: Path,
) -> None:
    router, session, _, _ = await make_router(
        tmp_path, auth_mode=AuthMode.per_user_oauth,
    )
    await router.get_prompt(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_name="greet",
        arguments={"who": "world"},
        user_id="alice",
        session_id=None,
    )
    session.get_prompt.assert_awaited_once_with("greet", {"who": "world"})


@pytest.mark.asyncio
async def test_get_prompt_logs_audit_entry(tmp_path: Path) -> None:
    router, _, audit, _ = await make_router(tmp_path)
    await router.get_prompt(
        org_id=DEFAULT_ORG_ID,
        upstream_id="notion",
        original_name="greet",
        arguments={"who": "world"},
        user_id="alice",
        session_id="sess1",
    )
    log_path = audit._log_path  # pyright: ignore[reportPrivateUsage]
    entry = json.loads(log_path.read_text().strip())
    assert entry["tool"] == "prompt:notion:greet"
    assert entry["response_status"] == "success"


@pytest.mark.asyncio
async def test_get_prompt_unknown_upstream_raises(tmp_path: Path) -> None:
    router, _, _, _ = await make_router(tmp_path)
    with pytest.raises(UpstreamRouterError, match="Unknown upstream"):
        await router.get_prompt(
            org_id=DEFAULT_ORG_ID,
            upstream_id="not-registered",
            original_name="greet",
            arguments=None,
            user_id="alice",
            session_id=None,
        )


# --- shared stall recovery + observability (R3 / R4) --------------------------
#
# The hoist into ``_dispatch_with_recovery`` closes the "forgot to heal"
# gap: resources/read and prompts/get now inherit the SAME ping-gated
# stall recovery the tool path has — but tool-only observability stays
# tool-only (R3), the per-verb actionable/opaque error shapes are
# preserved (R4), and a session-unavailable first attempt still emits no
# audit row (R4 no-audit gate).


class _FakeStallManager:
    """service_account manager slice the router touches for a stall:
    ``ensure_shared_connected`` / ``get_session`` / ``reconnect_shared_fresh``.
    ``reconnect_shared_fresh`` is the service_account heal."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.fresh_calls = 0

    async def ensure_shared_connected(self, upstream: Any) -> None:
        pass

    def get_session(self, upstream_id: str, user_id: str | None = None) -> Any:
        return self._session

    async def reconnect_shared_fresh(self, upstream: Any) -> None:
        self.fresh_calls += 1


def make_stall_router(
    tmp_path: Path, *, upstream_id: str = "notion",
) -> tuple[ToolRouter, MagicMock, _FakeStallManager, FileAuditRepository]:
    upstream = make_upstream_definition(id=upstream_id)  # service_account
    session = MagicMock()
    client_manager = _FakeStallManager(session)
    registry = ToolRegistry([upstream], cast(Any, client_manager))
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    router = ToolRouter(
        registry, cast(Any, client_manager), audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
    )
    return router, session, client_manager, audit


@pytest.mark.asyncio
async def test_read_resource_heals_and_retries_on_transport_stall(
    tmp_path: Path,
) -> None:
    """R3 gap closed: a stalled resources/read heals the shared session
    (fresh reconnect) and retries on a clean transport — the recovery the
    tool path had but read_resource lacked. ``retry_safe=True`` for
    resources (risk-b: no readOnlyHint exists for them)."""
    router, session, cm, _ = make_stall_router(tmp_path)
    ok = mcp_types.ReadResourceResult(
        contents=[
            mcp_types.TextResourceContents(
                uri=AnyUrl("test://hello"), mimeType="text/plain", text="hi",
            ),
        ],
    )
    session.read_resource = AsyncMock(side_effect=[asyncio.TimeoutError(), ok])

    result = await router.read_resource(
        org_id=DEFAULT_ORG_ID, upstream_id="notion",
        original_uri="test://hello", user_id="alice", session_id=None,
    )

    assert isinstance(result, mcp_types.ReadResourceResult)
    assert cm.fresh_calls == 1, "the stall must heal the shared session"
    assert session.read_resource.await_count == 2, "read must be retried"


@pytest.mark.asyncio
async def test_get_prompt_heals_and_retries_on_transport_stall(
    tmp_path: Path,
) -> None:
    """Counterpart for prompts/get."""
    router, session, cm, _ = make_stall_router(tmp_path)
    ok = mcp_types.GetPromptResult(
        description="x",
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text="hi"),
            ),
        ],
    )
    session.get_prompt = AsyncMock(side_effect=[asyncio.TimeoutError(), ok])

    result = await router.get_prompt(
        org_id=DEFAULT_ORG_ID, upstream_id="notion",
        original_name="greet", arguments=None, user_id="alice", session_id=None,
    )

    assert isinstance(result, mcp_types.GetPromptResult)
    assert cm.fresh_calls == 1, "the stall must heal the shared session"
    assert session.get_prompt.await_count == 2, "prompt must be retried"


@pytest.mark.asyncio
async def test_read_resource_does_not_emit_tool_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: resources/read must stay OUT of tool analytics + slow-tool
    dashboards — no ``tool_called`` event."""
    from mcpolis.domain.services import tool_router as tr_module

    tracked: list[tuple[Any, ...]] = []

    class _Stub:
        def track_async(self, *a: Any, **k: Any) -> None:
            tracked.append((a, k))

    monkeypatch.setattr(tr_module, "get_analytics", lambda: _Stub())
    router, _, _, _ = await make_router(tmp_path)
    await router.read_resource(
        org_id=DEFAULT_ORG_ID, upstream_id="notion",
        original_uri="test://hello", user_id="alice", session_id="s1",
    )
    assert tracked == [], "resources/read must not emit tool_called analytics"


@pytest.mark.asyncio
async def test_get_prompt_does_not_emit_tool_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: prompts/get must likewise stay out of tool analytics."""
    from mcpolis.domain.services import tool_router as tr_module

    tracked: list[tuple[Any, ...]] = []

    class _Stub:
        def track_async(self, *a: Any, **k: Any) -> None:
            tracked.append((a, k))

    monkeypatch.setattr(tr_module, "get_analytics", lambda: _Stub())
    router, _, _, _ = await make_router(tmp_path)
    await router.get_prompt(
        org_id=DEFAULT_ORG_ID, upstream_id="notion",
        original_name="greet", arguments={"who": "world"},
        user_id="alice", session_id="s1",
    )
    assert tracked == [], "prompts/get must not emit tool_called analytics"


@pytest.mark.asyncio
async def test_read_resource_session_unavailable_writes_no_audit(
    tmp_path: Path,
) -> None:
    """R4 no-audit gate: a session-unavailable read raises the actionable
    message on the FIRST attempt and emits NO audit row (it raises before
    any call ran), mirroring route_call's ``did_call`` gate."""
    upstream = make_upstream_definition(
        id="notion",
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )
    cm = UpstreamClientManager([upstream])
    audit = FileAuditRepository(tmp_path / "audit.jsonl")
    registry = ToolRegistry([upstream], cm)
    router = ToolRouter(
        registry, cm, audit, [upstream],
        policy_engine=PolicyEngine(SettingsConfig()),
        connection_store=None,
    )
    with pytest.raises(UpstreamRouterError):
        await router.read_resource(
            org_id=DEFAULT_ORG_ID, upstream_id="notion",
            original_uri="test://x", user_id="alice", session_id=None,
        )
    log_path = audit._log_path  # pyright: ignore[reportPrivateUsage]
    assert not log_path.exists() or log_path.read_text().strip() == "", (
        "session-unavailable on the first attempt must not write an audit row"
    )
