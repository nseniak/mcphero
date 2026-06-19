"""AUTH-7 — service tokens are rejected on the slug-scoped admin MCP.

Two layers of defense, both pinned here:

(a) **Structural** — the ``/admin-mcp`` app wraps the *raw* OAuth
    provider (``app.py:474``: ``BearerAuthBackend(provider)``). A
    ``svct_`` bearer never reaches the registry there, so it fails
    ``verify_token`` and the request 401s before any handler runs. This
    is the real production geometry.

(b) **Belt-and-braces** — even if the verifier wiring ever changed so a
    ``svct_`` *did* authenticate, ``admin_role_check`` (``app.py:547``)
    explicitly 403s any ``svc:`` identity with
    "Service tokens are not accepted on the admin MCP". That guard is
    otherwise dead code (layer (a) makes it unreachable); we exercise it
    by deliberately injecting a *composite* verifier that authenticates
    ``svct_`` tokens, proving the guard fires.

The admin app is built through the real ``_build_admin_app_with_oauth``
and mounted behind the real ``OrgContextMiddleware`` (cloud mode) so the
request travels the genuine ``/admin-mcp/<slug>/`` → slug-resolve →
rewrite-to-``/admin-mcp/`` path.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import TokenVerifier
from starlette.applications import Starlette
from starlette.routing import Mount

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.auth.service_token_verifier import (
    CompositeGatewayTokenVerifier,
    ServiceTokenVerifier,
)
from mcpolis.adapters.repositories.file_audit_repository import (
    FileAuditRepository,
)
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateRepository,
    OAuthStateSnapshot,
)
from mcpolis.domain.ports.organization_repository import (
    Membership,
    Organization,
)
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.app import _build_admin_app_with_oauth
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.controllers.admin_mcp_controller import (
    create_admin_mcp_server,
)
from mcpolis.entrypoints.middleware.org_context import (
    OrgContextMiddleware,
    SlugCache,
)
from tests.unit.factories import make_runtime_manager

ADMIN_EMAIL = "admin@example.com"
ORG_ID = "acme-id"
ORG_SLUG = "acme"


class _InMemoryOAuthStateRepository(OAuthStateRepository):
    def __init__(self) -> None:
        self._snapshot = OAuthStateSnapshot()

    async def load(self) -> OAuthStateSnapshot:
        return self._snapshot

    async def save(self, snapshot: OAuthStateSnapshot) -> None:
        self._snapshot = snapshot


class _InMemoryOrgRepo:
    """Minimal org repo so ``OrgService.resolve_slug('acme')`` resolves —
    same shape used across the multi-org gateway tests."""

    def __init__(
        self, orgs: list[Organization], memberships: list[Membership],
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


def make_admin_config() -> SettingsConfig:
    return SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True, settings=RoleSettings()),
        },
        users={ADMIN_EMAIL: UserDefinition(role="admin")},
    )


def make_org_service(config: SettingsConfig) -> OrgService:
    org = Organization(
        id=ORG_ID, slug=ORG_SLUG, display_name="Acme",
        created_at=datetime.now(UTC), created_by_email="creator@example.com",
    )
    membership = Membership(
        org_id=ORG_ID, email=ADMIN_EMAIL, role="admin",
        created_at=datetime.now(UTC),
    )
    org_repo = _InMemoryOrgRepo([org], [membership])
    config_repo = MagicMock()

    async def _load(*_args: object, **_kwargs: object) -> SettingsConfig:
        return config

    config_repo.load = _load
    return OrgService(org_repo=org_repo, config_repo=config_repo)  # type: ignore[arg-type]


def make_service_token_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(repo=FileServiceTokenRepository(tmp_path))


def make_cloud_admin_client(
    tmp_path: Path,
    *,
    verifier: TokenVerifier,
    config: SettingsConfig,
    org_service: OrgService,
) -> TestClient:
    """Build the real admin-MCP Starlette app guarded by *verifier* and
    mount it behind ``OrgContextMiddleware`` (cloud mode), so a POST to
    ``/admin-mcp/<slug>/`` travels the genuine slug-resolve path."""
    policy_engine = PolicyEngine(config)
    runtime_manager = make_runtime_manager(policy_engine, org_id=ORG_ID)
    audit_repo = FileAuditRepository(tmp_path / "data" / "audit.jsonl")
    config_store = FileConfigStore(tmp_path / "config.json")

    admin_mcp = create_admin_mcp_server(
        runtime_manager=runtime_manager,
        audit_repo=audit_repo,
        policy_store=config_store,
    )
    admin_starlette = admin_mcp.streamable_http_app()

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        server_url="http://localhost:8000",
    )
    guarded = _build_admin_app_with_oauth(
        admin_starlette, verifier, settings, runtime_manager,
    )

    parent = Starlette(routes=[Mount("/admin-mcp", app=guarded)])
    parent.add_middleware(
        OrgContextMiddleware,
        settings=settings,
        org_service=org_service,
        slug_cache=SlugCache(),
    )
    return TestClient(parent, raise_server_exceptions=False)


def make_raw_provider(
    tmp_path: Path, config: SettingsConfig,
) -> McpGatewayOAuthProvider:
    return McpGatewayOAuthProvider(
        google_client_id="",
        google_client_secret="",
        server_url="http://localhost:8000",
        runtime_manager=make_runtime_manager(PolicyEngine(config), org_id=ORG_ID),
        state_repository=_InMemoryOAuthStateRepository(),
    )


# ─────────────────────────── AUTH-7 (a) ────────────────────────────────


@pytest.mark.asyncio
async def test_svct_bearer_structurally_rejected_on_slug_scoped_admin_mcp(
    tmp_path: Path,
) -> None:
    """A ``svct_`` bearer presented to ``/admin-mcp/<slug>/`` 401s: the
    admin app wraps the raw OAuth provider, which never consults the
    service-token registry, so the token fails ``verify_token``."""
    config = make_admin_config()
    org_service = make_org_service(config)
    svc = make_service_token_service(tmp_path)
    minted = await svc.mint(
        org_id=ORG_ID, label="ci-bot", role_name="admin",
        created_by=ADMIN_EMAIL,
    )

    client = make_cloud_admin_client(
        tmp_path,
        verifier=make_raw_provider(tmp_path, config),
        config=config,
        org_service=org_service,
    )
    resp = client.post(
        f"/admin-mcp/{ORG_SLUG}/",
        headers={"Authorization": f"Bearer {minted.raw_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
    )
    assert resp.status_code == 401


# ─────────────────────────── AUTH-7 (b) ────────────────────────────────


@pytest.mark.asyncio
async def test_authenticated_svc_identity_hits_explicit_403_guard(
    tmp_path: Path,
) -> None:
    """Inject a *composite* verifier so a ``svct_`` token DOES
    authenticate — proving the otherwise-dead explicit guard in
    ``admin_role_check`` (``app.py:547``) fires: a ``svc:`` identity is
    403'd with the anti-service-token body, never granted admin access."""
    config = make_admin_config()
    org_service = make_org_service(config)
    svc = make_service_token_service(tmp_path)
    minted = await svc.mint(
        org_id=ORG_ID, label="ci-bot", role_name="admin",
        created_by=ADMIN_EMAIL,
    )

    # The composite authenticates svct_ tokens (registry path); the raw
    # OAuth fallback is never reached for an svct_ bearer.
    composite = CompositeGatewayTokenVerifier(
        ServiceTokenVerifier(svc),
        make_raw_provider(tmp_path, config),
    )
    client = make_cloud_admin_client(
        tmp_path,
        verifier=composite,
        config=config,
        org_service=org_service,
    )
    resp = client.post(
        f"/admin-mcp/{ORG_SLUG}/",
        headers={"Authorization": f"Bearer {minted.raw_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
    )
    assert resp.status_code == 403
    assert "Service tokens are not accepted on the admin MCP" in resp.text
