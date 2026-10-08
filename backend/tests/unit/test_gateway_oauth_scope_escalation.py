"""Attack test: a member forges service-token scopes through public OAuth.

The gateway's OAuth issuer lets any client self-register and request
any scope string. Service tokens used to carry their role and pinned
org as scopes (``mcpolis:svc``, ``mcpolis:role:<role>``,
``mcpolis:org:<id>``), and the gateway trusted those scopes whatever
token they rode on. So a member whose role grants no tools could
register a client asking for ``mcpolis:svc mcpolis:role:admin``, sign
in with Google, and get the admin role's tools. The consent step does
not stop this: the attacker approves their own client.

These drive the public flow over HTTP against the real gateway app:
register → authorize → Google callback → consent → token → tools/list.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.domain.ports.oauth_state_repository import StoredClient
from tests.unit._loopback_mcp import await_tools_ready
from tests.unit.test_gateway_oauth_consent import (
    consent_token_from,
    finish_google_sign_in,
)
from tests.unit.test_gateway_service_tokens import (
    _list_tools_with,  # pyright: ignore[reportPrivateUsage]
    _start_stack,  # pyright: ignore[reportPrivateUsage]
    _stop_stack,  # pyright: ignore[reportPrivateUsage]
)

# Literal strings on purpose: this is what an attacker types, and it
# must keep failing even if the constants are renamed or removed.
FORGED_SCOPE = "mcpolis:svc mcpolis:role:admin mcpolis:org:default"
REDIRECT_URI = "http://127.0.0.1:9/callback"
MEMBER = "member@example.com"  # role "none" in the harness config


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def make_registration_body(scope: str) -> dict[str, Any]:
    return {
        "redirect_uris": [REDIRECT_URI],
        "scope": scope,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def make_pre_fix_client(client_id: str, scope: str = FORGED_SCOPE) -> StoredClient:
    """A client as the provider stored it before registration refused
    reserved scopes — the shape an attacker may already have in prod."""
    return StoredClient(
        info=OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=[AnyUrl(REDIRECT_URI)],
            scope=scope,
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        registered_at=time.time(),
        token_issued=False,
    )


async def approve_if_asked(provider: McpGatewayOAuthProvider, url: str) -> str:
    """The attacker owns the client, so they approve the consent page
    when it shows. Returns the redirect carrying the code."""
    if "consent" not in parse_qs(urlparse(url).query):
        return url
    return await provider.resolve_consent(consent_token_from(url), approve=True)


async def sign_in(
    http: httpx.AsyncClient,
    provider: McpGatewayOAuthProvider,
    client_id: str,
    email: str,
) -> dict[str, Any]:
    """authorize (asking for the forged scopes) → Google → consent →
    token. Returns the token endpoint's JSON body."""
    verifier, challenge = make_pkce_pair()
    auth = await http.get("/mcp/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "attacker-state",
        "scope": FORGED_SCOPE,
    })
    location = auth.headers.get("location", "")
    assert location.startswith("https://accounts.google.com"), (
        auth.status_code, location,
    )
    google_state = parse_qs(urlparse(location).query)["state"][0]

    after_google = await finish_google_sign_in(provider, google_state, email)
    redirect = await approve_if_asked(provider, after_google)
    code = parse_qs(urlparse(redirect).query)["code"][0]

    tok = await http.post("/mcp/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    assert tok.status_code == 200, tok.text
    return dict(tok.json())


@pytest.mark.asyncio
async def test_registration_refuses_service_token_scopes(tmp_path: Path) -> None:
    """The front door: registering a client that asks for the forged
    scopes is refused outright; an ordinary scope still registers."""
    upstream_server, gateway_server, server_task, _app = await _start_stack(tmp_path)
    try:
        base_url = f"http://127.0.0.1:{gateway_server.config.port}"
        async with httpx.AsyncClient(base_url=base_url) as http:
            refused = await http.post(
                "/mcp/register", json=make_registration_body(FORGED_SCOPE),
            )
            assert refused.status_code == 400, refused.text
            assert refused.json()["error"] == "invalid_client_metadata"

            refused_upper = await http.post(
                "/mcp/register", json=make_registration_body("MCPOLIS:svc"),
            )
            assert refused_upper.status_code == 400, refused_upper.text

            accepted = await http.post(
                "/mcp/register", json=make_registration_body("read"),
            )
            assert accepted.status_code == 201, accepted.text
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)


@pytest.mark.asyncio
async def test_client_registered_before_the_fix_cannot_escalate(
    tmp_path: Path,
) -> None:
    """The full attack through a client that already holds the forged
    scopes (stored before registration refused them). The member gets a
    token without them and still sees no tools.

    Fails on the pre-fix code: the member's token keeps the forged
    scopes and its tools/list shows the admin role's tool."""
    upstream_server, gateway_server, server_task, gateway_app = (
        await _start_stack(tmp_path)
    )
    try:
        provider: McpGatewayOAuthProvider = gateway_app.state.mcp_gateway_oauth_provider
        base_url = f"http://127.0.0.1:{gateway_server.config.port}"

        # Controls: the admin sees the tool (so an empty list below
        # means "denied", not "stack not ready") and the member's own
        # role gives nothing.
        admin_token = await provider.mint_test_token("admin@example.com")
        await await_tools_ready(f"{base_url}/mcp/", admin_token, "fake__greet")
        assert await _list_tools_with(await provider.mint_test_token(MEMBER)) == []

        # mint_test_token loaded the provider state, so this seed sticks.
        provider._clients["pre-fix-client"] = make_pre_fix_client("pre-fix-client")  # pyright: ignore[reportPrivateUsage]

        async with httpx.AsyncClient(base_url=base_url) as http:
            token = await sign_in(http, provider, "pre-fix-client", MEMBER)

        assert "mcpolis:" not in (token.get("scope") or "")
        assert await _list_tools_with(str(token["access_token"])) == []
    finally:
        await _stop_stack(upstream_server, gateway_server, server_task)
