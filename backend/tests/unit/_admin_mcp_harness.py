"""Harness for the slug-scoped admin MCP and the superadmin MCP.

Each client builds its endpoint through the real
``_build_admin_app_with_oauth`` / ``_build_superadmin_app_with_oauth``
and mounts it behind the real ``OrgContextMiddleware`` (cloud mode), so
a request travels the production path: ``/admin-mcp/<slug>/`` resolves
the slug and rewrites to the ``/admin-mcp`` mount, while
``/admin-mcp/system/`` is a reserved segment left for its own mount.

Starlette's ``Mount`` never runs a mounted app's lifespan, so the parent
app's lifespan starts the endpoint's session manager, as the backend's
own lifespan does. Use the client as a context manager
(``with make_cloud_admin_client(...) as client:``) to run it; without
that, a request that gets past sign-in answers 500 ("Task group is not
initialized").
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from mcp.server.auth.provider import TokenVerifier
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.model.email_allowlist import EmailAllowlist
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.app import (
    _build_admin_app_with_oauth,
    _build_superadmin_app_with_oauth,
)
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from mcpolis.entrypoints.middleware.org_context import (
    OrgContextMiddleware,
    SlugCache,
)
from tests.unit._gateway_oauth_store import InMemoryOAuthStateRepository
from tests.unit.factories import make_runtime_manager
from tests.unit.test_multi_org_gateway import (
    InMemoryOrgRepo,
    make_membership,
    make_org,
)

ADMIN_EMAIL = "admin@example.com"
MEMBER_EMAIL = "member@example.com"
SUPERADMIN_EMAIL = "super@example.com"
ORG_ID = "acme-id"
ORG_SLUG = "acme"
SERVER_URL = "http://localhost:8000"

# What a streamable-HTTP MCP client sends with every POST; the SDK
# answers 406 to a POST that does not accept both.
MCP_ACCEPT = "application/json, text/event-stream"


def make_admin_config() -> SettingsConfig:
    return SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True, settings=RoleSettings()),
            "member": RoleDefinition(settings=RoleSettings()),
        },
        users={
            ADMIN_EMAIL: UserDefinition(role="admin"),
            MEMBER_EMAIL: UserDefinition(role="member"),
        },
    )


def make_org_repo() -> InMemoryOrgRepo:
    return InMemoryOrgRepo(
        orgs=[make_org(ORG_ID, ORG_SLUG, "Acme")],
        memberships=[make_membership(ORG_ID, ADMIN_EMAIL, role="admin")],
    )


def make_org_service(
    config: SettingsConfig, org_repo: InMemoryOrgRepo | None = None,
) -> OrgService:
    config_repo = MagicMock()

    async def _load(*_args: object, **_kwargs: object) -> SettingsConfig:
        return config

    config_repo.load = _load
    return OrgService(
        org_repo=org_repo or make_org_repo(),  # type: ignore[arg-type]
        config_repo=config_repo,
    )


def make_raw_provider(config: SettingsConfig) -> McpGatewayOAuthProvider:
    """The gateway OAuth provider, which both admin mounts wrap; its
    ``mint_test_token`` signs a user in without the Google round-trip."""
    return McpGatewayOAuthProvider(
        google_client_id="",
        google_client_secret="",
        server_url=SERVER_URL,
        runtime_manager=make_runtime_manager(PolicyEngine(config), org_id=ORG_ID),
        state_repository=InMemoryOAuthStateRepository(),
    )


def make_cloud_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        server_url=SERVER_URL,
    )


def make_initialize_body() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }


def make_signed_in_headers(
    token: str, site_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """What a signed-in streamable-HTTP MCP client sends, plus any
    site headers (Host, Origin) the test varies."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": MCP_ACCEPT,
        **(site_headers or {}),
    }


def make_session_manager_lifespan(
    server: FastMCP,
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    return lifespan


def make_cloud_client(
    *,
    mount_path: str,
    guarded_app: ASGIApp,
    server: FastMCP,
    settings: Settings,
    org_service: OrgService,
) -> TestClient:
    parent = Starlette(
        routes=[Mount(mount_path, app=guarded_app)],
        lifespan=make_session_manager_lifespan(server),
    )
    parent.add_middleware(
        OrgContextMiddleware,
        settings=settings,
        org_service=org_service,
        slug_cache=SlugCache(),
    )
    return TestClient(parent, raise_server_exceptions=False)


def make_cloud_admin_client(
    tmp_path: Path,
    *,
    verifier: TokenVerifier,
    config: SettingsConfig,
    org_service: OrgService,
) -> TestClient:
    """The real admin-MCP app guarded by *verifier*, mounted at
    ``/admin-mcp`` as the backend mounts it."""
    runtime_manager = make_runtime_manager(PolicyEngine(config), org_id=ORG_ID)
    admin_mcp = create_admin_mcp_server(
        runtime_manager=runtime_manager,
        audit_repo=FileAuditRepository(tmp_path / "data" / "audit.jsonl"),
        policy_store=FileConfigStore(tmp_path / "config.json"),
        template_var_repo=FileTemplateVarRepository(tmp_path / "data"),
    )
    settings = make_cloud_settings()
    guarded = _build_admin_app_with_oauth(
        admin_mcp, verifier, settings, runtime_manager,
    )
    return make_cloud_client(
        mount_path="/admin-mcp",
        guarded_app=guarded,
        server=admin_mcp,
        settings=settings,
        org_service=org_service,
    )


def make_cloud_superadmin_client(
    *, verifier: TokenVerifier, config: SettingsConfig,
) -> TestClient:
    """The real superadmin-MCP app (allowlist: ``SUPERADMIN_EMAIL``)
    guarded by *verifier*, mounted at ``/admin-mcp/system`` as the
    backend mounts it."""
    org_repo = make_org_repo()
    org_service = make_org_service(config, org_repo)
    superadmin_mcp = create_superadmin_mcp_server(
        org_repo=org_repo,  # type: ignore[arg-type]
        runtime_manager=make_runtime_manager(
            PolicyEngine(config), org_id=ORG_ID,
        ),
        org_service=org_service,
    )
    settings = make_cloud_settings()
    guarded = _build_superadmin_app_with_oauth(
        superadmin_mcp,
        verifier,
        settings,
        EmailAllowlist([SUPERADMIN_EMAIL]),
    )
    return make_cloud_client(
        mount_path="/admin-mcp/system",
        guarded_app=guarded,
        server=superadmin_mcp,
        settings=settings,
        org_service=org_service,
    )
