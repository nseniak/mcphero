"""Signed-in callers without the right role are refused on both admin MCP
endpoints, whatever the method.

The host check is off on these endpoints (``mcp_transport_security``),
so sign-in plus these checks are all that stand in front of the admin
tools: the org admin role on ``/admin-mcp/<slug>`` and the superadmin
email allowlist on ``/admin-mcp/system``. Each request carries the public
Host, so the 403 can only come from the role check.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._admin_mcp_harness import (
    ADMIN_EMAIL,
    MEMBER_EMAIL,
    ORG_SLUG,
    make_admin_config,
    make_cloud_admin_client,
    make_cloud_superadmin_client,
    make_initialize_body,
    make_org_service,
    make_raw_provider,
    make_signed_in_headers,
)

METHODS = ["GET", "POST", "DELETE"]
PUBLIC_SITE = {"Host": "mcphero.io"}


def make_body(method: str) -> dict[str, object] | None:
    return make_initialize_body() if method == "POST" else None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_admin_mcp_refuses_a_member_without_the_admin_role(
    tmp_path: Path, method: str,
) -> None:
    config = make_admin_config()
    provider = make_raw_provider(config)
    token = await provider.mint_test_token(MEMBER_EMAIL)

    with make_cloud_admin_client(
        tmp_path,
        verifier=provider,
        config=config,
        org_service=make_org_service(config),
    ) as client:
        resp = client.request(
            method, f"/admin-mcp/{ORG_SLUG}/",
            headers=make_signed_in_headers(token, PUBLIC_SITE),
            json=make_body(method),
        )

    assert resp.status_code == 403, resp.text
    assert "does not have admin role" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_superadmin_mcp_refuses_an_org_admin_off_the_allowlist(
    method: str,
) -> None:
    config = make_admin_config()
    provider = make_raw_provider(config)
    token = await provider.mint_test_token(ADMIN_EMAIL)

    with make_cloud_superadmin_client(verifier=provider, config=config) as client:
        resp = client.request(
            method, "/admin-mcp/system/",
            headers=make_signed_in_headers(token, PUBLIC_SITE),
            json=make_body(method),
        )

    assert resp.status_code == 403, resp.text
    assert "Not a superadmin" in resp.text
