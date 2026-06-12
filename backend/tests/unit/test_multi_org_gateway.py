"""Multi-org cloud-mode gateway: fixed /mcp URL + tools aggregated across orgs.

Pins the *new* behavior introduced by the "fixed cloud /mcp URL +
multi-org tool aggregation" change. Older OAuth tests in
test_google_oauth.py still cover single-org/standalone semantics; this
file is exclusively about the cloud multi-org gateway.

Test style: top-level functions, dependency-injected helpers, no
fixtures, no classes (per CLAUDE.md).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
    StoredAuthCode,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import (
    DiscoveredTool,
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)
from mcpolis.domain.ports.organization_repository import (
    Membership,
    Organization,
)
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from tests.unit.factories import make_full_access_config


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class InMemoryOAuthStateRepository(OAuthStateRepository):
    """Single global snapshot — matches the new user-scoped contract."""

    def __init__(self) -> None:
        self._snapshot = OAuthStateSnapshot()

    async def load(self) -> OAuthStateSnapshot:
        return self._snapshot

    async def save(self, snapshot: OAuthStateSnapshot) -> None:
        self._snapshot = snapshot


class InMemoryOrgRepo:
    """Minimal OrganizationRepository stand-in for tests.

    Only implements the methods the OAuth provider + OrgService touch
    in this test file. We deliberately avoid the full repository impl —
    no Mongo, no file IO.
    """

    def __init__(
        self,
        *,
        orgs: list[Organization],
        memberships: list[Membership],
    ) -> None:
        self._orgs = {o.id: o for o in orgs}
        self._slugs = {o.slug: o for o in orgs}
        self._memberships = memberships

    async def get_organization(self, org_id: str) -> Organization | None:
        return self._orgs.get(org_id)

    async def get_by_slug(self, slug: str) -> Organization | None:
        return self._slugs.get(slug)

    async def get_memberships_for_email(
        self, email: str,
    ) -> list[Membership]:
        return [m for m in self._memberships if m.email == email]

    async def list_organizations(self) -> list[Organization]:
        return list(self._orgs.values())


def make_org(org_id: str, slug: str, display_name: str) -> Organization:
    return Organization(
        id=org_id,
        slug=slug,
        display_name=display_name,
        created_at=datetime.now(UTC),
        created_by_email="creator@test.com",
    )


def make_membership(org_id: str, email: str, role: str = "default") -> Membership:
    return Membership(
        org_id=org_id,
        email=email,
        role=role,
        created_at=datetime.now(UTC),
    )


def make_runtime_manager_with_orgs(
    orgs: list[tuple[str, list[str]]],
    *,
    user_emails: list[str] | None = None,
) -> OrgRuntimeManager:
    """Build a runtime manager with one runtime per org.

    ``orgs`` is a list of (org_id, [upstream_ids]). Each runtime gets
    a stub tool_registry that returns a tool per upstream named
    ``{upstream_id}__do_thing``. Each per-org policy engine is seeded
    with a full-access role for ``user_emails`` (default: alice) —
    PolicyEngine fails closed on zero roles, so plumbing tests need a
    real grant. Policy filtering itself has dedicated tests; this
    file is about multi-org aggregation / routing.
    """
    emails = user_emails if user_emails is not None else ["alice@test.com"]

    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )

    for org_id, upstream_ids in orgs:
        upstreams = [
            UpstreamDefinition(
                id=uid,
                display_name=uid.title(),
                transport=TransportType.streamable_http,
                http=HttpTransportConfig(url=f"http://upstream.invalid/{uid}"),
                auth=UpstreamAuthConfig(mode=AuthMode.service_account),
            )
            for uid in upstream_ids
        ]
        tool_registry = MagicMock()
        tool_registry.get_upstream_ids = MagicMock(return_value=upstream_ids)
        tools = [
            DiscoveredTool(
                upstream_id=uid,
                original_name="do_thing",
                prefixed_name=f"{uid}__do_thing",
                description=f"do something on {uid}",
                input_schema={"type": "object", "properties": {}},
            )
            for uid in upstream_ids
        ]
        tool_registry.get_tools_for_upstreams = MagicMock(return_value=tools)
        # Real method: convert DiscoveredTool to MCP Tool. Use an
        # actual ToolRegistry instance for this rather than mocking,
        # so the tools come back as real mcp_types.Tool with correct
        # name/description.
        from mcpolis.domain.services.tool_registry import ToolRegistry

        real_tr = ToolRegistry(upstreams, MagicMock())
        tool_registry.to_mcp_tools = real_tr.to_mcp_tools

        runtime = OrgRuntime(
            org_id=org_id,
            policy_engine=PolicyEngine(
                make_full_access_config(upstream_ids, emails),
            ),
            tool_registry=tool_registry,
            client_manager=MagicMock(),
            tool_router=MagicMock(),
            config_service=MagicMock(),
            upstreams=upstreams,
        )
        manager._runtimes[org_id] = runtime
        manager._startup_status[org_id] = MagicMock(
            ready=True, total=0, connected=set(), failed=set(),
        )
    return manager


def make_provider_with_org_service(
    org_repo: InMemoryOrgRepo,
    *,
    config_repo: Any | None = None,
) -> McpGatewayOAuthProvider:
    """Build a McpGatewayOAuthProvider wired to an OrgService, for the
    multi-org any-org policy check in handle_google_callback."""
    from mcpolis.domain.services.org_service import OrgService

    if config_repo is None:
        config_repo = MagicMock()
        config_repo.load = AsyncMock(return_value=SettingsConfig())
        config_repo.ensure_defaults = AsyncMock(return_value=SettingsConfig())

    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config_repo,
    )

    manager = OrgRuntimeManager(
        config_repo=config_repo,
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )

    return McpGatewayOAuthProvider(
        google_client_id="test-client-id",
        google_client_secret="test-secret",
        server_url="http://localhost:8000",
        runtime_manager=manager,
        state_repository=InMemoryOAuthStateRepository(),
        org_service=org_service,
    )


# ---------------------------------------------------------------------------
# 1. Token model — user-scoped
# ---------------------------------------------------------------------------


def test_stored_access_token_has_no_org_id_field() -> None:
    """StoredAccessToken is now user-scoped: no org_id."""
    fields = {f.name for f in StoredAccessToken.__dataclass_fields__.values()}
    assert "org_id" not in fields, (
        "StoredAccessToken must drop org_id (user-scoped tokens)"
    )


def test_stored_refresh_token_has_no_org_id_field() -> None:
    fields = {f.name for f in StoredRefreshToken.__dataclass_fields__.values()}
    assert "org_id" not in fields, (
        "StoredRefreshToken must drop org_id (user-scoped tokens)"
    )


def test_oauth_state_repository_load_takes_no_org_id() -> None:
    """The repository contract is now a single global snapshot.

    The previous per-org partitioning was a leftover from the slug-in-
    URL design. With a fixed cloud /mcp URL there is exactly one
    OAuth namespace.
    """
    import inspect

    sig = inspect.signature(OAuthStateRepository.load)
    # ``self`` only — no org_id.
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert params == [], (
        f"OAuthStateRepository.load must take no parameters; got {params}"
    )

    sig_save = inspect.signature(OAuthStateRepository.save)
    save_params = [
        p.name for p in sig_save.parameters.values() if p.name != "self"
    ]
    assert save_params == ["snapshot"], (
        f"OAuthStateRepository.save signature must be (snapshot); "
        f"got {save_params}"
    )


# ---------------------------------------------------------------------------
# 2. OAuth provider — global namespace, any-org policy check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_token_visible_regardless_of_active_org() -> None:
    """Tokens are user-scoped: a token issued for alice loads no matter
    what org context the request happens to be in."""
    from mcpolis.entrypoints.controllers.gateway_controller import (
        current_org_id,
    )

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "alice@test.com"),
        ],
    )
    provider = make_provider_with_org_service(org_repo)

    client = OAuthClientInformationFull(
        client_id="cid",
        redirect_uris=[AnyUrl("http://localhost/cb")],
    )
    await provider.register_client(client)

    code = StoredAuthCode(
        client_id="cid",
        user_email="alice@test.com",
        code_challenge="x",
        redirect_uri=AnyUrl("http://localhost/cb"),
        redirect_uri_provided_explicitly=True,
        scopes=None,
        state="s",
        created_at=time.time(),
        expires_at=time.time() + 300,
    )
    provider._auth_codes["c"] = code
    token = await provider.exchange_authorization_code(client, code)

    # Active org context shouldn't matter for token validation.
    for active in ("acme-id", "beta-id", DEFAULT_ORG_ID, MULTI_ORG_SENTINEL):
        ctx = current_org_id.set(active)
        try:
            loaded = await provider.load_access_token(token.access_token)
            assert loaded is not None, (
                f"token must be loadable when active org is {active!r}"
            )
            assert loaded.client_id == "alice@test.com"
        finally:
            current_org_id.reset(ctx)


@pytest.mark.asyncio
async def test_oauth_callback_allows_user_with_any_org_membership() -> None:
    """In cloud mode the callback policy check is 'is the user a member
    of any org?' — not 'is the user a member of the URL-resolved org?'.
    """
    from unittest.mock import patch

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
        ],
    )
    provider = make_provider_with_org_service(org_repo)

    client = OAuthClientInformationFull(
        client_id="cid",
        redirect_uris=[AnyUrl("http://localhost/cb")],
    )
    await provider.register_client(client)

    from mcp.server.auth.provider import AuthorizationParams

    params = AuthorizationParams(
        state="s",
        scopes=None,
        code_challenge="x",
        redirect_uri=AnyUrl("http://localhost/cb"),
        redirect_uri_provided_explicitly=True,
    )
    await provider.authorize(client, params)
    google_state = next(iter(provider._pending_auths))

    import base64
    import json

    def _make_id_token(email: str) -> str:
        h = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
        p = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=")
        s = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        return f"{h.decode()}.{p.decode()}.{s.decode()}"

    fake_id_token = _make_id_token("alice@test.com")
    mock_response = AsyncMock()
    mock_response.json = lambda: {"id_token": fake_id_token}
    mock_response.raise_for_status = lambda: None

    with patch(
        "mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        # Should succeed: alice is a member of acme.
        redirect_url = await provider.handle_google_callback(
            "google-code", google_state,
        )
    assert "code=" in redirect_url


@pytest.mark.asyncio
async def test_oauth_callback_denies_user_with_no_org_membership() -> None:
    from unittest.mock import patch

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
        ],
    )
    provider = make_provider_with_org_service(org_repo)

    client = OAuthClientInformationFull(
        client_id="cid",
        redirect_uris=[AnyUrl("http://localhost/cb")],
    )
    await provider.register_client(client)

    from mcp.server.auth.provider import AuthorizationParams

    params = AuthorizationParams(
        state="s",
        scopes=None,
        code_challenge="x",
        redirect_uri=AnyUrl("http://localhost/cb"),
        redirect_uri_provided_explicitly=True,
    )
    await provider.authorize(client, params)
    google_state = next(iter(provider._pending_auths))

    import base64
    import json

    def _make_id_token(email: str) -> str:
        h = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
        p = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=")
        s = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        return f"{h.decode()}.{p.decode()}.{s.decode()}"

    fake_id_token = _make_id_token("nobody@test.com")
    mock_response = AsyncMock()
    mock_response.json = lambda: {"id_token": fake_id_token}
    mock_response.raise_for_status = lambda: None

    with patch(
        "mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(ValueError, match="not authorized"):
            await provider.handle_google_callback("google-code", google_state)


# ---------------------------------------------------------------------------
# 3. Middleware — MULTI_ORG_SENTINEL for cloud /mcp/...
# ---------------------------------------------------------------------------


def test_multi_org_sentinel_constant_exists() -> None:
    """MULTI_ORG_SENTINEL identifies a cloud /mcp request that should
    fan out across the user's orgs at the gateway boundary."""
    assert isinstance(MULTI_ORG_SENTINEL, str)
    assert MULTI_ORG_SENTINEL != DEFAULT_ORG_ID
    # Use a sentinel value that can never collide with a real slug.
    # Real slugs are validated by ``_SLUG_PATTERN`` (a-z0-9-) so a
    # double-underscore wrapped name is safe.
    assert MULTI_ORG_SENTINEL.startswith("__")


@pytest.mark.asyncio
async def test_middleware_sets_multi_org_sentinel_for_cloud_mcp_path() -> None:
    """In cloud mode, /mcp/... (no slug, no reserved segment) gets the
    MULTI_ORG_SENTINEL — gateway controller fans out from there."""
    from starlette.types import Message

    from mcpolis.domain.services.org_service import OrgService
    from mcpolis.entrypoints.config import Settings
    from mcpolis.entrypoints.controllers.gateway_controller import (
        current_org_id,
    )
    from mcpolis.entrypoints.middleware.org_context import (
        OrgContextMiddleware,
        SlugCache,
    )

    captured: dict[str, str] = {}

    async def dummy_app(scope: Any, receive: Any, send: Any) -> None:
        captured["org_id"] = current_org_id.get()
        captured["path"] = scope.get("path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    settings = Settings(mode="cloud", server_url="http://localhost:8000")
    org_service = OrgService(
        org_repo=MagicMock(),
        config_repo=MagicMock(),
    )
    middleware = OrgContextMiddleware(
        app=dummy_app,
        settings=settings,
        org_service=org_service,
        slug_cache=SlugCache(),
    )

    scope: dict[str, Any] = {
        "type": "http",
        "path": "/mcp/",
        "raw_path": b"/mcp/",
        "method": "POST",
        "headers": [],
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    sent: list[Message] = []

    async def send(msg: Message) -> None:
        sent.append(msg)

    await middleware(scope, receive, send)

    assert captured["org_id"] == MULTI_ORG_SENTINEL, (
        f"expected MULTI_ORG_SENTINEL, got {captured['org_id']!r}"
    )


@pytest.mark.asyncio
async def test_middleware_resolves_slug_for_admin_mcp_in_cloud() -> None:
    """Admin MCP keeps its /admin-mcp/{slug}/... shape — only /mcp gets
    a fixed URL."""
    from starlette.types import Message

    from mcpolis.entrypoints.config import Settings
    from mcpolis.entrypoints.controllers.gateway_controller import (
        current_org_id,
    )
    from mcpolis.entrypoints.middleware.org_context import (
        OrgContextMiddleware,
        SlugCache,
    )

    captured: dict[str, str] = {}

    async def dummy_app(scope: Any, receive: Any, send: Any) -> None:
        captured["org_id"] = current_org_id.get()
        captured["path"] = scope.get("path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    org_service = MagicMock()

    async def _resolve_slug(slug: str) -> Organization:
        if slug == "acme":
            return make_org("acme-id", "acme", "Acme")
        from mcpolis.domain.services.org_service import OrgNotFoundError
        raise OrgNotFoundError(slug)

    org_service.resolve_slug = _resolve_slug

    settings = Settings(mode="cloud", server_url="http://localhost:8000")
    middleware = OrgContextMiddleware(
        app=dummy_app,
        settings=settings,
        org_service=org_service,
        slug_cache=SlugCache(),
    )

    scope: dict[str, Any] = {
        "type": "http",
        "path": "/admin-mcp/acme/foo",
        "raw_path": b"/admin-mcp/acme/foo",
        "method": "POST",
        "headers": [],
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    sent: list[Message] = []

    async def send(msg: Message) -> None:
        sent.append(msg)

    await middleware(scope, receive, send)
    assert captured["org_id"] == "acme-id"
    assert captured["path"] == "/admin-mcp/foo"


# ---------------------------------------------------------------------------
# 4. Gateway controller — list_tools aggregation, call_tool routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_aggregates_across_user_orgs_with_slug_prefix() -> None:
    """When current_org_id is MULTI_ORG_SENTINEL, list_tools fans out
    over the user's orgs and prefixes each tool with the org's slug.
    """
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from mcpolis.domain.services.org_service import OrgService
    from mcpolis.entrypoints.controllers.gateway_controller import (
        create_mcp_server,
        current_org_id,
    )

    # Two orgs, alice is a member of both.
    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={
            "default": RoleDefinition(is_default=True, settings=RoleSettings()),
        },
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config_repo,
    )

    manager = make_runtime_manager_with_orgs([
        ("acme-id", ["gmail", "github"]),
        ("beta-id", ["slack"]),
    ])
    manager.register_display_name("acme-id", "Acme")
    manager.register_display_name("beta-id", "Beta")

    server = create_mcp_server(manager, org_service=org_service)

    # Set up the request context: alice + multi-org sentinel.
    auth_user = AuthenticatedUser(
        AccessToken(
            token="fake",
            client_id="alice@test.com",
            scopes=[],
            expires_at=int(time.time()) + 3600,
        )
    )
    auth_token = auth_context_var.set(auth_user)
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        # Hit the registered list_tools handler.
        # Server.list_tools is registered via a decorator; the handler
        # is reachable through the request_handlers dict.
        from mcp import types as mcp_types

        handler = server.request_handlers[mcp_types.ListToolsRequest]
        request = mcp_types.ListToolsRequest(method="tools/list")
        result = await handler(request)
        list_result = cast(mcp_types.ListToolsResult, result.root)
        tool_names = sorted(t.name for t in list_result.tools)
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)

    assert tool_names == sorted([
        "acme__gmail__do_thing",
        "acme__github__do_thing",
        "beta__slack__do_thing",
    ]), tool_names


@pytest.mark.asyncio
async def test_list_tools_scoped_to_single_org_when_current_org_id_pinned() -> None:
    """Same alice as the merge-mode test, but the request comes through
    ``/mcp/{slug}`` so ``current_org_id`` is the resolved org (not the
    multi-org sentinel). The gateway must surface only that org's tools
    and use the 2-part ``{upstream}__{tool}`` naming consistent with
    ``/admin-mcp/{slug}``."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from mcpolis.domain.services.org_service import OrgService
    from mcpolis.entrypoints.controllers.gateway_controller import (
        create_mcp_server,
        current_org_id,
    )

    # alice is a member of both acme and beta — same setup as the
    # merge-mode test on purpose, so the contrast is just current_org_id.
    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={
            "default": RoleDefinition(is_default=True, settings=RoleSettings()),
        },
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config_repo,
    )

    manager = make_runtime_manager_with_orgs([
        ("acme-id", ["gmail", "github"]),
        ("beta-id", ["slack"]),
    ])
    manager.register_display_name("acme-id", "Acme")
    manager.register_display_name("beta-id", "Beta")

    server = create_mcp_server(manager, org_service=org_service)

    auth_user = AuthenticatedUser(
        AccessToken(
            token="fake",
            client_id="alice@test.com",
            scopes=[],
            expires_at=int(time.time()) + 3600,
        )
    )
    auth_token = auth_context_var.set(auth_user)
    # Slug-scoped request: middleware pinned org_id to acme.
    org_token = current_org_id.set("acme-id")
    try:
        from mcp import types as mcp_types

        handler = server.request_handlers[mcp_types.ListToolsRequest]
        request = mcp_types.ListToolsRequest(method="tools/list")
        result = await handler(request)
        list_result = cast(mcp_types.ListToolsResult, result.root)
        tool_names = sorted(t.name for t in list_result.tools)
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)

    # Only acme's tools, 2-part naming (no org-slug prefix).
    assert tool_names == sorted([
        "gmail__do_thing",
        "github__do_thing",
    ]), tool_names


@pytest.mark.asyncio
async def test_call_tool_routes_by_org_slug_prefix() -> None:
    """A 3-part-prefixed tool call splits off the org slug, resolves
    to the right runtime, and forwards the inner {upstream}__{tool}
    name to that runtime's tool router."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from mcp import types as mcp_types

    from mcpolis.domain.services.org_service import OrgService
    from mcpolis.entrypoints.controllers.gateway_controller import (
        create_mcp_server,
        current_org_id,
    )

    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
            make_membership("beta-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={
            "default": RoleDefinition(is_default=True, settings=RoleSettings()),
        },
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config_repo,
    )

    manager = make_runtime_manager_with_orgs([
        ("acme-id", ["gmail"]),
        ("beta-id", ["slack"]),
    ])

    # Capture which runtime's tool_router was called and with what
    # prefixed_name.
    acme_router_calls: list[tuple[str, str, dict[str, Any]]] = []
    beta_router_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def acme_route_call(
        org_id: str, prefixed_name: str, arguments: dict[str, Any], **_: Any,
    ) -> Any:
        acme_router_calls.append((org_id, prefixed_name, arguments))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok-acme")],
        )

    async def beta_route_call(
        org_id: str, prefixed_name: str, arguments: dict[str, Any], **_: Any,
    ) -> Any:
        beta_router_calls.append((org_id, prefixed_name, arguments))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok-beta")],
        )

    manager._runtimes["acme-id"].tool_router.route_call = acme_route_call  # pyright: ignore[reportAttributeAccessIssue]
    manager._runtimes["beta-id"].tool_router.route_call = beta_route_call  # pyright: ignore[reportAttributeAccessIssue]

    server = create_mcp_server(manager, org_service=org_service)

    auth_user = AuthenticatedUser(
        AccessToken(
            token="fake",
            client_id="alice@test.com",
            scopes=[],
            expires_at=int(time.time()) + 3600,
        )
    )
    auth_token = auth_context_var.set(auth_user)
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.CallToolRequest]
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="beta__slack__do_thing",
                arguments={"x": 1},
            ),
        )
        await handler(request)
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)

    assert beta_router_calls == [("beta-id", "slack__do_thing", {"x": 1})]
    assert acme_router_calls == []


@pytest.mark.asyncio
async def test_call_tool_rejects_org_slug_user_is_not_a_member_of() -> None:
    """A user can't invent a slug — calling a tool prefixed with an org
    they don't belong to is rejected as access denied."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken
    from mcp import types as mcp_types

    from mcpolis.domain.services.org_service import OrgService
    from mcpolis.entrypoints.controllers.gateway_controller import (
        create_mcp_server,
        current_org_id,
    )

    # alice is only a member of acme — beta exists but she's not in it.
    org_repo = InMemoryOrgRepo(
        orgs=[
            make_org("acme-id", "acme", "Acme"),
            make_org("beta-id", "beta", "Beta"),
        ],
        memberships=[
            make_membership("acme-id", "alice@test.com"),
        ],
    )
    config_repo = MagicMock()
    config_repo.load = AsyncMock(return_value=SettingsConfig(
        roles={
            "default": RoleDefinition(is_default=True, settings=RoleSettings()),
        },
        users={"alice@test.com": UserDefinition(role="default")},
    ))
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config_repo,
    )

    manager = make_runtime_manager_with_orgs([
        ("acme-id", ["gmail"]),
        ("beta-id", ["slack"]),
    ])

    beta_router_calls: list[Any] = []

    async def beta_route_call(*args: Any, **kwargs: Any) -> Any:
        beta_router_calls.append((args, kwargs))
        return mcp_types.CallToolResult(content=[])

    manager._runtimes["beta-id"].tool_router.route_call = beta_route_call  # pyright: ignore[reportAttributeAccessIssue]

    server = create_mcp_server(manager, org_service=org_service)

    auth_user = AuthenticatedUser(
        AccessToken(
            token="fake",
            client_id="alice@test.com",
            scopes=[],
            expires_at=int(time.time()) + 3600,
        )
    )
    auth_token = auth_context_var.set(auth_user)
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        handler = server.request_handlers[mcp_types.CallToolRequest]
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="beta__slack__do_thing",
                arguments={},
            ),
        )
        result = await handler(request)
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)

    # Beta's tool_router was never called.
    assert beta_router_calls == []

    # The result is an access-denied error.
    call_result = cast(mcp_types.CallToolResult, result.root)
    blocks = call_result.content
    assert len(blocks) == 1
    text_block = blocks[0]
    assert isinstance(text_block, mcp_types.TextContent)
    assert "access denied" in text_block.text.lower() or (
        "not a member" in text_block.text.lower()
    )


# ---------------------------------------------------------------------------
# 5. Tool-name length cap
# ---------------------------------------------------------------------------


def test_slug_validation_rejects_slug_too_long_for_3_part_prefix() -> None:
    """Org slugs are user-chosen and end up in {slug}__{upstream}__{tool}.
    The slug validator must reject anything that, combined with a worst-
    case upstream + tool name, would exceed the 64-char MCP tool name cap.
    """
    from mcpolis.domain.services.org_service import (
        SlugValidationError,
        validate_slug,
    )

    # A slug right at the existing 40-char ceiling.
    long_slug = "a" + "b" * 38 + "c"
    assert len(long_slug) == 40
    # Should now fail because 40 + len("__") + max(upstream+tool) blows
    # the 64-char tool name cap. Exact bound is documented in the
    # validator.
    with pytest.raises(SlugValidationError):
        validate_slug(long_slug)


def test_slug_validation_accepts_short_slug() -> None:
    from mcpolis.domain.services.org_service import validate_slug

    # Should not raise.
    validate_slug("acme")
    validate_slug("my-team")


# ---------------------------------------------------------------------------
# 6. /api/config/gateway returns fixed URL in cloud mode
# ---------------------------------------------------------------------------


def test_session_instructions_personalized_for_single_org_user() -> None:
    """User with one org gets a tailored welcome that names the team
    and the slug, instead of the generic multi-org string."""
    from mcpolis.entrypoints.controllers.gateway_controller import (
        _instructions_for_user_orgs,
    )

    only_org = make_org("acme-id", "acme", "Acme Corp")
    instructions = _instructions_for_user_orgs([only_org])

    assert "Acme Corp" in instructions
    assert "acme__" in instructions  # slug appears in the example
    assert "every organization you belong to" not in instructions


def test_session_instructions_lists_org_names_for_multi_org_user() -> None:
    """User in N>=2 orgs gets a string that enumerates the org names so
    the LLM can correlate tool prefixes to teams without scanning."""
    from mcpolis.entrypoints.controllers.gateway_controller import (
        _instructions_for_user_orgs,
    )

    orgs = [
        make_org("acme-id", "acme", "Acme Corp"),
        make_org("beta-id", "beta", "Beta LLC"),
    ]
    instructions = _instructions_for_user_orgs(orgs)

    assert "Acme Corp" in instructions
    assert "Beta LLC" in instructions
    # The example uses the first org's slug — pinned so the LLM sees a
    # concrete tool name shape that matches the actual prefix scheme.
    assert "acme__" in instructions


def test_session_instructions_falls_back_for_zero_orgs() -> None:
    """Defensive: 0 orgs shouldn't happen in production (the OAuth
    callback rejects it) but the helper must not crash."""
    from mcpolis.entrypoints.controllers.gateway_controller import (
        _MULTI_ORG_INSTRUCTIONS,
        _instructions_for_user_orgs,
    )

    assert _instructions_for_user_orgs([]) == _MULTI_ORG_INSTRUCTIONS


def test_gateway_config_url_is_slug_scoped_in_cloud_mode() -> None:
    """Pin: ``/api/config/gateway`` returns the slug-scoped
    ``{server_url}/mcp/{slug}`` URL in cloud mode. The bare ``/mcp``
    URL still works for clients that want to merge tools across every
    org (see middleware sentinel branch) but is intentionally not
    surfaced through this dashboard endpoint."""
    from mcpolis.entrypoints.routes.dashboard._deps import (
        compose_user_mcp_url,
    )

    # cloud mode: slug-scoped URL surfaced to the user.
    assert compose_user_mcp_url(
        server_url="https://server.example",
        is_cloud_mode=True,
        org_slug="acme",
    ) == "https://server.example/mcp/acme"

    # cloud mode without a slug (e.g. standalone-style setup that
    # somehow lacks a slug) falls back to the bare URL.
    assert compose_user_mcp_url(
        server_url="https://server.example",
        is_cloud_mode=True,
        org_slug="",
    ) == "https://server.example/mcp"

    # standalone mode: bare URL, single-org install, no slug needed.
    assert compose_user_mcp_url(
        server_url="https://server.example",
        is_cloud_mode=False,
        org_slug="default",
    ) == "https://server.example/mcp"


def test_effective_gateway_url_falls_back_to_server_url_when_unset() -> None:
    """Default deploy: gateway and dashboard share an origin. Empty
    ``gateway_url`` resolves to ``server_url`` so existing single-origin
    deploys are unaffected by the subdomain-split feature."""
    from mcpolis.entrypoints.config import Settings, effective_gateway_url

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        server_url="https://mcphero.io",
        gateway_url="",
    )

    assert effective_gateway_url(settings) == "https://mcphero.io"


def test_effective_gateway_url_uses_explicit_subdomain_when_set() -> None:
    """Subdomain-split deploy: ``MCPOLIS_GATEWAY_URL`` overrides the
    apex ``MCPOLIS_SERVER_URL`` for gateway-side URL construction."""
    from mcpolis.entrypoints.config import Settings, effective_gateway_url

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        server_url="https://mcphero.io",
        gateway_url="https://mcp.mcphero.io",
    )

    assert effective_gateway_url(settings) == "https://mcp.mcphero.io"


def test_effective_gateway_url_strips_trailing_slash() -> None:
    """Operators write env vars different ways; the helper normalises
    so callers can build paths with a plain ``f"{base}/mcp"`` regardless
    of how the env var was written."""
    from mcpolis.entrypoints.config import Settings, effective_gateway_url

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        server_url="https://mcphero.io/",
        gateway_url="https://mcp.mcphero.io/",
    )

    assert effective_gateway_url(settings) == "https://mcp.mcphero.io"
