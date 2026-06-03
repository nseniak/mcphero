"""Shared test factories — call make_XXX() explicitly in each test."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
from pydantic import AnyHttpUrl, AnyUrl

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    DiscoveredTool,
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    RefreshFailureSignature,
)


def make_upstream_auth(
    mode: AuthMode = AuthMode.service_account,
    token: str | None = None,
) -> UpstreamAuthConfig:
    return UpstreamAuthConfig(mode=mode, token=token)


def make_upstream_definition(
    id: str = "test-upstream",
    display_name: str = "Test Upstream",
    transport: TransportType | None = None,
    command: str = "echo",
    url: str = "http://localhost:9999/mcp",
    auth: UpstreamAuthConfig | None = None,
    **kwargs: Any,
) -> UpstreamDefinition:
    if auth is None:
        auth = make_upstream_auth()
    # ``transport`` defaults to stdio for the historic
    # service_account case (most upstream tests just want a generic
    # upstream and don't care about transport). When the caller
    # passes an OAuth ``auth`` without naming a transport, default to
    # ``streamable_http`` instead — stdio + OAuth is a non-functional
    # shape rejected by the model validator (see
    # ``test_stdio_auth_mode_invariant.py``).
    if transport is None:
        transport = (
            TransportType.streamable_http
            if auth.mode != AuthMode.service_account
            else TransportType.stdio
        )
    if transport == TransportType.stdio:
        return UpstreamDefinition(
            id=id,
            display_name=display_name,
            transport=transport,
            stdio=StdioTransportConfig(command=command),
            auth=auth,
            **kwargs,
        )
    return UpstreamDefinition(
        id=id,
        display_name=display_name,
        transport=transport,
        http=HttpTransportConfig(url=url),
        auth=auth,
        **kwargs,
    )


def make_discovered_tool(
    upstream_id: str = "test-upstream",
    original_name: str = "do_thing",
    description: str = "Does a thing",
    input_schema: dict[str, Any] | None = None,
) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name=original_name,
        prefixed_name=f"{upstream_id}__{original_name}",
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
    )


def make_runtime_manager(
    policy_engine: PolicyEngine,
    tool_registry: ToolRegistry | None = None,
    client_manager: UpstreamClientManager | None = None,
    tool_router: ToolRouter | None = None,
    config_service: UpstreamConfigService | None = None,
    upstreams: list[UpstreamDefinition] | None = None,
    org_id: str = "default",
) -> OrgRuntimeManager:
    """Build an OrgRuntimeManager with a single pre-loaded runtime for tests."""
    from unittest.mock import MagicMock

    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=policy_engine,
        tool_registry=tool_registry or MagicMock(),
        client_manager=client_manager or MagicMock(),
        tool_router=tool_router or MagicMock(),
        config_service=config_service or MagicMock(),
        upstreams=upstreams or [],
    )
    manager._runtimes[org_id] = runtime
    manager._startup_status[org_id] = MagicMock(ready=True, total=0, connected=set(), failed=set())
    return manager


def make_oauth_upstream(
    id: str = "notion",
    display_name: str = "Notion",
    mode: AuthMode = AuthMode.admin_oauth,
    url: str = "https://mcp.example.invalid/mcp",
) -> UpstreamDefinition:
    """OAuth-mode upstream with a streamable_http transport.

    Consolidates the ``_make_upstream`` helper that was duplicated
    across ``test_upstream_oauth_silent_refresh.py``,
    ``test_refresh_failure_signature.py``,
    ``test_refresh_failure_policy.py``, ``test_liveness_probe.py``,
    and ``test_upstream_health_check.py``. Each copy defaulted to
    slightly different display_name / id combinations — keep a
    single shape here so any "standard OAuth upstream" test doesn't
    have to re-decide.
    """
    return UpstreamDefinition(
        id=id,
        display_name=display_name,
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=url),
        auth=UpstreamAuthConfig(mode=mode),
    )


def make_stored_oauth_token(
    access_token: str = "stored-at",
    refresh_token: str | None = "stored-rt",
    *,
    expires_in_minutes: float = 30.0,
) -> OAuthToken:
    """An ``OAuthToken`` with a configurable expiry offset.

    Positive ``expires_in_minutes`` → valid for that many minutes
    from now; negative → already expired. The OAuth-durability
    tests use both signs depending on whether they're exercising
    the silent-refresh or already-expired branch."""
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )


async def seed_oauth_storage(
    store: FileConnectionStore,
    *,
    org_id: str = DEFAULT_ORG_ID,
    upstream_id: str = "notion",
    user_id: str = "__admin__",
    callback_url: str = "https://gateway.example.invalid/api/oauth/upstream/callback",
    expires_in_minutes: float = 30.0,
) -> OAuthToken:
    """Drop a user token + client_info into a connection store so a
    real ``_build_oauth_provider`` has everything it needs.

    Returns the seeded token so tests can assert identity
    post-reconnect. The client_info's ``redirect_uris`` must include
    ``callback_url`` — otherwise ``_build_oauth_provider``'s
    DCR self-heal path drops client_info and the SDK's
    ``can_refresh_token`` silently returns False, bypassing the
    refresh branch we want to pin in tests.
    """
    token = make_stored_oauth_token(expires_in_minutes=expires_in_minutes)
    await store.put_user_token(org_id, user_id, upstream_id, token)
    await store.put_client_info(
        org_id, upstream_id, user_id,
        OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=[AnyUrl(callback_url)],
            token_endpoint_auth_method="client_secret_post",
        ).model_dump(mode="json"),
    )
    return token


def make_oauth_metadata(
    issuer: str = "https://oauth.example.invalid",
    authorization_endpoint: str | None = None,
    token_endpoint: str | None = None,
    registration_endpoint: str | None = None,
) -> OAuthMetadata:
    """Build an RFC 8414 ``OAuthMetadata`` whose ``token_endpoint`` is
    deliberately on a different host from the upstream MCP base URL.

    The §3.8 / §5.4 bug is specifically about Mixpanel-style upstreams
    whose token endpoint isn't ``<base>/token``: the SDK's refresh
    branch falls back to that path and 404s. Defaulting the endpoints
    to ``oauth.example.invalid`` (separate from the upstream's
    ``mcp.example.invalid``) keeps tests honest about that geometry —
    a fix that "works" only when token_endpoint == <base>/token would
    silently pass an asymmetric test.
    """
    base = issuer.rstrip("/")
    return OAuthMetadata(
        issuer=AnyHttpUrl(base),
        authorization_endpoint=AnyHttpUrl(
            authorization_endpoint or f"{base}/authorize",
        ),
        token_endpoint=AnyHttpUrl(token_endpoint or f"{base}/oauth/token"),
        registration_endpoint=(
            AnyHttpUrl(registration_endpoint)
            if registration_endpoint is not None
            else None
        ),
    )


def make_refresh_failure_signature(
    error_code: str | None = "invalid_grant",
    *,
    status_code: int | None = None,
    body_excerpt: str | None = None,
    timestamp: datetime | None = None,
) -> RefreshFailureSignature:
    """Build a ``RefreshFailureSignature`` with sensible defaults for
    each branch of the §5.1 delete-vs-retry policy.

    Defaults land on ``invalid_grant`` + status 400 (the
    notify/delete case). Pass ``error_code=None`` for the
    transient-5xx case — status falls through to 500.
    """
    if status_code is None:
        status_code = 400 if error_code == "invalid_grant" else 500
    if body_excerpt is None:
        body_excerpt = (
            '{"error":"invalid_grant"}' if error_code == "invalid_grant"
            else "<html>bad gateway</html>"
        )
    return RefreshFailureSignature(
        status_code=status_code,
        body_excerpt=body_excerpt,
        error_code=error_code,
        timestamp=timestamp or datetime.now(UTC),
    )


def make_audit_entry(
    user_id: str = "testuser",
    upstream_id: str = "test-upstream",
    tool: str = "test-upstream__do_thing",
    **kwargs: Any,
) -> AuditEntry:
    defaults: dict[str, Any] = {
        "timestamp": "2026-01-01T00:00:00Z",
        "user_id": user_id,
        "upstream_id": upstream_id,
        "auth_mode": "service_account",
        "auth_identity": f"service_account:{upstream_id}",
        "tool": tool,
        "policy_decision": "allowed",
        "response_status": "success",
        "latency_ms": 42.0,
        "session_id": None,
    }
    defaults.update(kwargs)
    return AuditEntry(**defaults)


