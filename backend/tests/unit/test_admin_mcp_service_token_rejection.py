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
rewrite-to-``/admin-mcp/`` path (``tests/unit/_admin_mcp_harness.py``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.auth.service_token_verifier import (
    CompositeGatewayTokenVerifier,
    ServiceTokenVerifier,
)
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from tests.unit._admin_mcp_harness import (
    ADMIN_EMAIL,
    ORG_ID,
    ORG_SLUG,
    make_admin_config,
    make_cloud_admin_client,
    make_initialize_body,
    make_org_service,
    make_raw_provider,
)


def make_service_token_service(tmp_path: Path) -> ServiceTokenService:
    return ServiceTokenService(repo=FileServiceTokenRepository(tmp_path))


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
        verifier=make_raw_provider(config),
        config=config,
        org_service=org_service,
    )
    resp = client.post(
        f"/admin-mcp/{ORG_SLUG}/",
        headers={"Authorization": f"Bearer {minted.raw_token}"},
        json=make_initialize_body(),
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
        make_raw_provider(config),
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
        json=make_initialize_body(),
    )
    assert resp.status_code == 403
    assert "Service tokens are not accepted on the admin MCP" in resp.text
