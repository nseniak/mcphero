"""SEC-CONFUSED-DEPUTY, part 1 — registrable redirect URI tightening.

Open dynamic client registration let any client register *any*
``redirect_uri``. The cleartext-to-remote case (``http://`` to a host
that isn't loopback) ships an authorization code over the wire in the
clear to an arbitrary server and is never legitimate, so
``register_client`` must reject it with the SDK's ``invalid_redirect_uri``
(→ a clean 400, not a 500).

What stays allowed, because real clients depend on it and the consent
step (part 2) is what actually stops the confused-deputy attack:

- ``https://…`` to any host — Claude.ai web connectors
  (``https://claude.ai/api/mcp/auth_callback``). A remote ``https`` host
  cannot be blocked at registration without breaking web connectors;
  consent is the gate that catches a hostile one.
- loopback ``http://`` — Claude Code / Cursor local listeners
  (``http://127.0.0.1:PORT``, ``localhost``, ``[::1]``).
- private-use URI schemes (``cursor://…``) — native-app redirects per
  RFC 8252.
"""
from __future__ import annotations

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import _redirect_identity
from tests.unit.test_google_oauth import make_provider


def make_client_with_redirects(
    *redirect_uris: str, client_id: str = "c-reg"
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(u) for u in redirect_uris],
    )


# ── allowed: real clients must keep working ──────────────────────────


@pytest.mark.asyncio
async def test_register_allows_https_remote_redirect() -> None:
    """Claude.ai web connector callback — remote https, must be allowed."""
    provider = make_provider()
    client = make_client_with_redirects(
        "https://claude.ai/api/mcp/auth_callback"
    )
    await provider.register_client(client)
    assert await provider.get_client("c-reg") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1:1455/callback",
        "http://localhost:33418/oauth/callback",
        "http://[::1]:5000/callback",
    ],
)
async def test_register_allows_loopback_http_redirect(uri: str) -> None:
    """Claude Code / Cursor local listeners — loopback http, allowed."""
    provider = make_provider()
    await provider.register_client(make_client_with_redirects(uri))
    assert await provider.get_client("c-reg") is not None


@pytest.mark.asyncio
async def test_register_allows_private_use_scheme_redirect() -> None:
    """Native-app private-use URI scheme (RFC 8252) — allowed."""
    provider = make_provider()
    client = make_client_with_redirects(
        "cursor://anysphere.cursor-retrieval/oauth/callback"
    )
    await provider.register_client(client)
    assert await provider.get_client("c-reg") is not None


# ── rejected: cleartext to a remote host ─────────────────────────────


@pytest.mark.asyncio
async def test_register_rejects_http_remote_redirect() -> None:
    """``http://`` to a non-loopback host ships a code in cleartext to an
    arbitrary server — rejected with ``invalid_redirect_uri``."""
    provider = make_provider()
    client = make_client_with_redirects("http://evil.example.com/grab")

    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client)
    assert exc.value.error == "invalid_redirect_uri"


@pytest.mark.asyncio
async def test_register_rejects_when_any_redirect_is_cleartext_remote() -> None:
    """One bad URI in the set fails the whole registration — an attacker
    can't smuggle a cleartext-remote URI in beside a good one."""
    provider = make_provider()
    client = make_client_with_redirects(
        "https://good.example.com/cb",
        "http://evil.example.com/grab",
    )

    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client)
    assert exc.value.error == "invalid_redirect_uri"

    # And nothing was persisted for the rejected client.
    assert await provider.get_client("c-reg") is None


@pytest.mark.asyncio
async def test_register_rejects_redirect_with_userinfo() -> None:
    """Review finding 1. A redirect whose authority embeds userinfo
    (``https://accounts.google.com@evil.com``) reads as one host but the
    browser delivers to another — it would let the consent page show a
    host the code never reaches. Rejected at registration."""
    provider = make_provider()
    client = make_client_with_redirects(
        "https://accounts.google.com@evil.com/cb"
    )
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client)
    assert exc.value.error == "invalid_redirect_uri"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(document.domain)",
        "data:text/html,<script>x</script>",
        "file:///etc/passwd",
    ],
)
async def test_register_rejects_hostless_schemes(uri: str) -> None:
    """Review finding 4. Non-http schemes are allowed only when they name
    a host (RFC 8252 private-use, e.g. ``cursor://host``). Hostless
    schemes are not a real redirect destination — rejected."""
    provider = make_provider()
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(make_client_with_redirects(uri))
    assert exc.value.error == "invalid_redirect_uri"


def test_redirect_identity_drops_userinfo_and_port() -> None:
    """The approval/display identity is scheme+host only: userinfo and
    port are stripped, so it reflects the true delivery host."""
    assert (
        _redirect_identity("https://accounts.google.com@evil.com/cb")
        == "https://evil.com"
    )
    assert _redirect_identity("http://127.0.0.1:1455/cb") == "http://127.0.0.1"
    assert _redirect_identity("http://127.0.0.1:2222/x") == "http://127.0.0.1"
    assert (
        _redirect_identity("https://claude.ai/api/mcp/auth_callback")
        == "https://claude.ai"
    )
