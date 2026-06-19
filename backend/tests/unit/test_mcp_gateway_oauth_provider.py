"""Gateway OAuth provider — edge cases on the Google delegation path.

Covers the two guardrails the audit flagged on
``McpGatewayOAuthProvider`` (``adapters/auth/mcp_gateway_oauth_provider``):

- **AUTH-4** — ``extract_email_from_id_token`` must return ``None`` for a
  malformed JWT (wrong part count, bad base64, missing ``email`` claim)
  and the callback must turn a no-email token into a *clean* ``ValueError``,
  never an unhandled crash.
- **AUTH-12** [BUG?] — when Google's token endpoint misbehaves (HTTP 500,
  a 200 with no ``id_token``, or a connection timeout) the callback must
  surface a *handled* error, not let a raw ``httpx`` exception escape as an
  unhandled 500.

The Google token exchange is driven through a real ``httpx.MockTransport``
injected into the provider's ``httpx.AsyncClient`` so the exception
geometry is the SDK's own, not a hand-rolled mock's.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from unittest.mock import patch

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
    extract_email_from_id_token,
)
from tests.unit.test_google_oauth import (
    make_auth_params,
    make_client,
    make_id_token,
    make_provider,
)

# Keep the real class so the MockTransport-injecting wrapper isn't
# recursive (patching ``httpx.AsyncClient`` with a factory that itself
# constructs an ``httpx.AsyncClient`` would otherwise recurse forever).
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def make_id_token_without_email() -> str:
    """A structurally valid 3-part JWT whose payload JSON lacks ``email``."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "12345"}).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"fake-sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


async def drive_callback_with_handler(
    provider: McpGatewayOAuthProvider,
    handler,  # noqa: ANN001 — httpx.MockTransport handler signature
) -> str:
    """Authorize, then run ``handle_google_callback`` with Google's token
    endpoint served by *handler* (an ``httpx.MockTransport`` callback).

    Returns the callback's redirect URL on success; the caller asserts on
    the exception when one is expected.
    """
    client = make_client()
    await provider.register_client(client)
    await provider.authorize(client, make_auth_params())
    google_state = next(iter(provider._pending_auths))

    transport = httpx.MockTransport(handler)

    def patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

    with patch(
        "mcpolis.adapters.auth.mcp_gateway_oauth_provider.httpx.AsyncClient",
        patched_client,
    ):
        return await provider.handle_google_callback("google-code", google_state)


# ─────────────────────────── AUTH-4 ────────────────────────────────────


def test_extract_email_two_part_token_returns_none() -> None:
    """A token with !=3 dot-separated parts is not a JWT → ``None``."""
    assert extract_email_from_id_token("a.b") is None


def test_extract_email_bad_base64_payload_returns_none() -> None:
    """A 3-part token whose payload isn't valid base64-JSON → ``None``
    (the decode raises inside the ``try`` and is swallowed)."""
    assert extract_email_from_id_token("a.@@.c") is None


def test_extract_email_payload_without_email_claim_returns_none() -> None:
    """A valid 3-part JWT whose payload omits ``email`` → ``None``."""
    assert extract_email_from_id_token(make_id_token_without_email()) is None


@pytest.mark.asyncio
async def test_callback_no_email_token_raises_clean_value_error() -> None:
    """The callback path must surface a *clean* ``ValueError`` — not an
    unhandled exception — when Google returns a token with no email.

    Google returns HTTP 200 with a syntactically valid ``id_token`` whose
    payload lacks ``email``; ``extract_email_from_id_token`` returns
    ``None`` and the provider raises the documented ValueError at
    ``mcp_gateway_oauth_provider.py:311-312``.
    """
    provider = make_provider()
    tok = make_id_token_without_email()

    with pytest.raises(ValueError, match="Could not extract email"):
        await drive_callback_with_handler(
            provider,
            lambda request: httpx.Response(200, json={"id_token": tok}),
        )


@pytest.mark.asyncio
async def test_callback_happy_path_still_works() -> None:
    """Control: a well-formed Google response yields a redirect with a
    fresh auth code — proves the MockTransport harness, not just the
    error branches, drives the real exchange."""
    provider = make_provider()
    tok = make_id_token("alice@test.com")

    redirect = await drive_callback_with_handler(
        provider,
        lambda request: httpx.Response(200, json={"id_token": tok}),
    )
    assert "http://localhost:3000/callback" in redirect
    assert "code=" in redirect
    assert len(provider._auth_codes) == 1


# ─────────────────────────── AUTH-12 [BUG?] ────────────────────────────
#
# Intended contract: a misbehaving Google token endpoint (5xx, a 200 with
# no id_token, or a network timeout) is a third-party fault the gateway
# must *handle* — the OAuth callback should fail with a clean error, never
# let a raw httpx exception bubble out as an unhandled 500.
#
# Observed: ``handle_google_callback`` does ``resp.raise_for_status()``
# (``mcp_gateway_oauth_provider.py:302``) with no surrounding try/except,
# and ``http_client.post`` is likewise unguarded. So:
#   - HTTP 500  → ``httpx.HTTPStatusError`` escapes (unhandled).
#   - timeout   → ``httpx.ConnectTimeout`` escapes (unhandled).
# Only the "200 but no id_token" case is handled (clean ValueError at
# :307-308). The two unguarded cases are the bug; the tests below pin the
# intended *handled* contract and xfail-strict until the prod path catches
# the httpx failures.


@pytest.mark.asyncio
async def test_callback_google_500_is_handled_not_unhandled() -> None:
    """A 500 from Google's token endpoint must become a handled error
    (a ValueError the callback route can map to a clean response), not a
    raw ``httpx.HTTPStatusError``."""
    provider = make_provider()

    with pytest.raises(ValueError):
        await drive_callback_with_handler(
            provider,
            lambda request: httpx.Response(500, json={"error": "boom"}),
        )


@pytest.mark.asyncio
async def test_callback_google_200_without_id_token_raises_value_error() -> None:
    """A 200 with an empty body (no ``id_token``) is already handled: the
    provider raises a clean ValueError at :307-308. Pins the one branch
    that *is* correct so a future fix to the 500/timeout branches can't
    regress it."""
    provider = make_provider()

    with pytest.raises(ValueError, match="did not return an ID token"):
        await drive_callback_with_handler(
            provider,
            lambda request: httpx.Response(200, json={}),
        )


@pytest.mark.asyncio
async def test_callback_google_timeout_is_handled_not_unhandled() -> None:
    """A network timeout talking to Google must become a handled error,
    not a raw ``httpx.ConnectTimeout``."""
    provider = make_provider()

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ValueError):
        await drive_callback_with_handler(provider, timeout_handler)


@pytest.mark.asyncio
async def test_callback_google_malformed_200_body_is_handled_not_unhandled() -> None:
    """A 200 with a non-JSON body must become a handled ValueError too —
    ``resp.json()`` raises ``json.JSONDecodeError`` inside the token
    exchange, which the broadened ``except`` maps to the clean ValueError
    family rather than relying on the callback route to catch a raw
    JSONDecodeError."""
    provider = make_provider()

    with pytest.raises(ValueError):
        await drive_callback_with_handler(
            provider,
            lambda request: httpx.Response(200, content=b"<not json>"),
        )
