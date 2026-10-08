"""Both admin MCP endpoints answer whatever Host the client asked for.

FastMCP turns on the SDK's DNS-rebinding protection by itself whenever it
is built with its default host (127.0.0.1): every request whose Host is
not localhost gets 421 "Invalid Host header", and every browser Origin
that is not localhost gets 403. Production nginx forwards the public
Host (``proxy_set_header Host $host``), so ``/admin-mcp/<slug>`` and
``/admin-mcp/system`` refused every real client. Dev and e2e never saw
it: the Vite proxy rewrites Host to the backend's loopback address and
e2e calls the backend on 127.0.0.1.

Each request here is signed in with a real gateway bearer, so it gets
past sign-in and the role / allowlist checks; only the site headers vary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit._admin_mcp_harness import (
    ADMIN_EMAIL,
    ORG_SLUG,
    SUPERADMIN_EMAIL,
    make_admin_config,
    make_cloud_admin_client,
    make_cloud_superadmin_client,
    make_initialize_body,
    make_org_service,
    make_raw_provider,
    make_signed_in_headers,
)

SITE_HEADERS: dict[str, dict[str, str]] = {
    "localhost": {"Host": "localhost:8000"},
    "public-site": {"Host": "mcphero.io"},
    "proxied-dev-site": {"Host": "tunnel.example.com"},
    # Localhost Host, so only the Origin check can refuse it (403).
    "browser-client": {
        "Host": "localhost:8000",
        "Origin": "https://inspector.example.com",
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("site", list(SITE_HEADERS))
async def test_admin_mcp_answers_initialize_for_any_site(
    tmp_path: Path, site: str,
) -> None:
    config = make_admin_config()
    provider = make_raw_provider(config)
    token = await provider.mint_test_token(ADMIN_EMAIL)

    with make_cloud_admin_client(
        tmp_path,
        verifier=provider,
        config=config,
        org_service=make_org_service(config),
    ) as client:
        resp = client.post(
            f"/admin-mcp/{ORG_SLUG}/",
            headers=make_signed_in_headers(token, SITE_HEADERS[site]),
            json=make_initialize_body(),
        )

    assert resp.status_code == 200, resp.text
    assert '"name":"MCP Hero Admin' in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("site", list(SITE_HEADERS))
async def test_superadmin_mcp_answers_initialize_for_any_site(site: str) -> None:
    config = make_admin_config()
    provider = make_raw_provider(config)
    token = await provider.mint_test_token(SUPERADMIN_EMAIL)

    with make_cloud_superadmin_client(verifier=provider, config=config) as client:
        resp = client.post(
            "/admin-mcp/system/",
            headers=make_signed_in_headers(token, SITE_HEADERS[site]),
            json=make_initialize_body(),
        )

    assert resp.status_code == 200, resp.text
    assert '"name":"MCP Hero Superadmin"' in resp.text
