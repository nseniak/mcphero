"""SEC-CONFUSED-DEPUTY, part 2 — the consent gate.

The attack: an attacker registers a client whose ``redirect_uri`` points
at a host they control (a *remote https* host, which registration cannot
block — see part 1), PKCE-pairs it, and sends a victim the
``/mcp/authorize`` link. The victim picks their real Google account and,
today, the gateway forwards the authorization code straight to the
attacker's ``redirect_uri`` — the attacker exchanges it for the victim's
gateway token (the MCP spec's "confused deputy").

The fix: after Google tells us *who* the victim is, and before any code
is issued, the gateway stops at its own consent page naming the client
and the host the code would be sent to. No code leaves without an
explicit approval, and the approval is remembered per (user, client) so
a real client only prompts once.

These tests drive the provider directly; the Google token exchange is
served by an ``httpx.MockTransport`` so the exchange geometry is the
SDK's own.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from unittest.mock import patch
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from tests.unit.test_google_oauth import make_id_token, make_provider

_REAL_ASYNC_CLIENT = httpx.AsyncClient

ATTACKER_REDIRECT = "https://attacker.example/grab"
ATTACKER_HOST = "attacker.example"


def make_client_with(
    client_id: str = "c-1",
    redirect_uri: str = ATTACKER_REDIRECT,
    client_name: str | None = "Totally Legit MCP",
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=[AnyUrl(redirect_uri)],
    )


def make_params_for(
    client: OAuthClientInformationFull, redirect_uri: str | None = None
) -> AuthorizationParams:
    assert client.redirect_uris is not None
    return AuthorizationParams(
        state="victim-state",
        scopes=None,
        code_challenge="victim-challenge",
        redirect_uri=AnyUrl(redirect_uri) if redirect_uri else client.redirect_uris[0],
        redirect_uri_provided_explicitly=True,
    )


async def run_google_callback(
    provider: McpGatewayOAuthProvider,
    client: OAuthClientInformationFull,
    email: str,
    redirect_uri: str | None = None,
) -> str:
    """Register (idempotent), authorize, and complete the Google callback
    for *client* authenticating as *email*. Returns whatever URL the
    callback resolves to (client redirect, or the consent page).

    *redirect_uri* overrides which registered redirect the authorize
    request uses (defaults to the client's first)."""
    await provider.register_client(client)
    await provider.authorize(client, make_params_for(client, redirect_uri))
    google_state = next(reversed(provider._pending_auths))
    return await finish_google_sign_in(provider, google_state, email)


async def finish_google_sign_in(
    provider: McpGatewayOAuthProvider, google_state: str, email: str,
) -> str:
    """Google's callback for the pending sign-in *google_state*, with
    Google (served by a mock) answering that *email* signed in."""
    tok = make_id_token(email)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id_token": tok})
    )

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    with patch(
        "mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient",
        patched,
    ):
        return await provider.handle_google_callback("g-code", google_state)


def consent_token_from(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    return q["consent"][0]


# ── the core guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unapproved_client_lands_on_consent_not_client_redirect() -> None:
    """The confused-deputy regression test. A first-time client does NOT
    get the code: the callback resolves to the gateway's own consent page,
    the attacker host never appears, and no auth code has been minted."""
    provider = make_provider()
    redirect = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )

    assert "/mcp/oauth/consent" in redirect
    assert ATTACKER_HOST not in redirect
    assert len(provider._auth_codes) == 0


@pytest.mark.asyncio
async def test_consent_prompt_names_client_and_redirect_host() -> None:
    """The page the victim sees must name the client and the exact host
    the code would be sent to — that naming is what lets them refuse."""
    provider = make_provider()
    redirect = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )

    prompt = await provider.render_consent(consent_token_from(redirect))
    assert prompt is not None
    assert prompt.client_name == "Totally Legit MCP"
    # The shown host is the true scheme+host (no userinfo/port spoofing).
    assert prompt.redirect_host == f"https://{ATTACKER_HOST}"
    assert ATTACKER_HOST in prompt.redirect_uri


@pytest.mark.asyncio
async def test_consent_approve_issues_code_to_client() -> None:
    """Explicit approval releases the code to the client's redirect."""
    provider = make_provider()
    redirect = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )
    token = consent_token_from(redirect)

    final = await provider.resolve_consent(token, approve=True)
    assert final.startswith(ATTACKER_REDIRECT)
    assert "code=" in final
    assert len(provider._auth_codes) == 1


@pytest.mark.asyncio
async def test_consent_deny_issues_no_code() -> None:
    """Denial mints no code and records no approval."""
    provider = make_provider()
    redirect = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )
    token = consent_token_from(redirect)

    final = await provider.resolve_consent(token, approve=False)
    assert "code=" not in final
    assert len(provider._auth_codes) == 0
    assert not await provider.is_client_approved(
        "victim@test.com", "c-1", ATTACKER_REDIRECT
    )


@pytest.mark.asyncio
async def test_approved_client_skips_consent_next_time() -> None:
    """Once approved, the same (user, client) is forwarded directly —
    the remembered approval is what keeps real clients to one prompt."""
    provider = make_provider()
    first = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )
    await provider.resolve_consent(consent_token_from(first), approve=True)

    second = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )
    assert second.startswith(ATTACKER_REDIRECT)
    assert "code=" in second
    assert "/mcp/oauth/consent" not in second


@pytest.mark.asyncio
async def test_approval_is_scoped_to_the_user() -> None:
    """alice approving a client does not let bob skip consent for it."""
    provider = make_provider()
    first = await run_google_callback(
        provider, make_client_with(), "alice@test.com"
    )
    await provider.resolve_consent(consent_token_from(first), approve=True)

    other = await run_google_callback(
        provider, make_client_with(), "bob@test.com"
    )
    assert "/mcp/oauth/consent" in other


@pytest.mark.asyncio
async def test_approval_is_scoped_to_the_client() -> None:
    """Approving client A does not skip consent for a different client B,
    even for the same user and the same redirect host."""
    provider = make_provider()
    first = await run_google_callback(
        provider, make_client_with(client_id="A"), "victim@test.com"
    )
    await provider.resolve_consent(consent_token_from(first), approve=True)

    other = await run_google_callback(
        provider, make_client_with(client_id="B"), "victim@test.com"
    )
    assert "/mcp/oauth/consent" in other


@pytest.mark.asyncio
async def test_approval_does_not_leak_across_redirect_hosts() -> None:
    """Review finding 2. A client registers two redirects (a trusted
    brand host and an exfil host). Approving via the brand host must NOT
    skip consent when the same client later uses the exfil host — the
    approval binds to the host the victim saw, not the client id."""
    provider = make_provider()
    client = OAuthClientInformationFull(
        client_id="multi",
        client_name="Brandy",
        redirect_uris=[
            AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            AnyUrl("https://exfil.evil/grab"),
        ],
    )
    await provider.register_client(client)
    # Victim approves the brand host.
    await provider.record_client_approval(
        "victim@test.com", "multi", "https://claude.ai/api/mcp/auth_callback"
    )

    # Same client, same user, but now delivering to the exfil host.
    redirect = await run_google_callback(
        provider, client, "victim@test.com",
        redirect_uri="https://exfil.evil/grab",
    )
    assert "/mcp/oauth/consent" in redirect
    assert "exfil.evil" not in redirect
    assert len(provider._auth_codes) == 0


@pytest.mark.asyncio
async def test_loopback_approval_is_port_insensitive() -> None:
    """A native client approved on one ephemeral loopback port is NOT
    re-prompted when it next listens on a different port — the approval
    identity is scheme+host, so Claude Code / Cursor prompt once."""
    provider = make_provider()
    await provider.record_client_approval(
        "dev@test.com", "cli", "http://127.0.0.1:1111/callback"
    )
    assert await provider.is_client_approved(
        "dev@test.com", "cli", "http://127.0.0.1:2222/callback"
    )


@pytest.mark.asyncio
async def test_consent_token_is_single_use() -> None:
    """A consent token can't be replayed to mint a second code."""
    provider = make_provider()
    redirect = await run_google_callback(
        provider, make_client_with(), "victim@test.com"
    )
    token = consent_token_from(redirect)

    await provider.resolve_consent(token, approve=True)
    with pytest.raises(ValueError):
        await provider.resolve_consent(token, approve=True)


@pytest.mark.asyncio
async def test_unknown_consent_token_is_rejected() -> None:
    provider = make_provider()
    assert await provider.render_consent("bogus") is None
    with pytest.raises(ValueError):
        await provider.resolve_consent("bogus", approve=True)
