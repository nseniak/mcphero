"""Tests for ``RefreshFailureSignature`` capture in ``_InitializingOAuthClientProvider``.

Motivation (from ``internal/documents/oauth-durability.md`` §5.4): when an
upstream ``refresh_token`` grant fails and our outer ``except`` path
deletes the stored tokens, the one signal that would let a future
session distinguish *genuine* revocation from a transient 5xx or
network blip — the response's status code and body — goes only into
the SDK's WARNING log and is then lost. The delete path destroys the
evidence. Without this signal, §5.1's less-trigger-happy delete
policy has nothing to branch on.

The fix: ``_InitializingOAuthClientProvider`` overrides
``_handle_refresh_response``. Before the SDK's default handler clears
context tokens, we read a bounded excerpt of the body and stash the
triplet ``(status_code, body_excerpt, error_code)`` on
``self.last_refresh_failure``. Tests here drive the real SDK
generator (same pattern as ``test_upstream_oauth_silent_refresh.py``)
so a regression can't sneak past a hand-built ``OAuthContext``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    REFRESH_FAILURE_BODY_LIMIT,
    RefreshFailureSignature,
    _InitializingOAuthClientProvider,
    _build_oauth_provider,
    _noop_callback,
    _noop_redirect,
)
from tests.unit.factories import make_oauth_upstream, seed_oauth_storage

UPSTREAM_ID = "notion"
USER_ID = "__admin__"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"
CALLBACK_URL = f"{SERVER_URL}/api/oauth/upstream/callback"


async def _make_provider(tmp_path: Path) -> _InitializingOAuthClientProvider:
    store = FileConnectionStore(tmp_path)
    await seed_oauth_storage(
        store,
        upstream_id=UPSTREAM_ID,
        user_id=USER_ID,
        callback_url=CALLBACK_URL,
        expires_in_minutes=-5,
    )
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, USER_ID)
    provider = await _build_oauth_provider(
        make_oauth_upstream(
            id=UPSTREAM_ID, display_name="Notion",
            mode=AuthMode.per_user_oauth, url=UPSTREAM_URL,
        ),
        storage,
        _noop_redirect, _noop_callback,
        SERVER_URL,
    )
    assert isinstance(provider, _InitializingOAuthClientProvider)
    return provider


# ── Direct subclass-level tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_new_provider_has_no_refresh_failure_signature(
    tmp_path: Path,
) -> None:
    """A fresh provider has nothing captured. The attribute must exist
    and be ``None`` so callers can test it without an ``AttributeError``."""
    provider = await _make_provider(tmp_path)
    assert provider.last_refresh_failure is None


@pytest.mark.asyncio
async def test_handle_refresh_response_400_invalid_grant_captures_signature(
    tmp_path: Path,
) -> None:
    """A 400 with a JSON body containing ``error: invalid_grant`` is
    the canonical "refresh token genuinely dead" signal. The signature
    must preserve both the status and the parsed ``error_code`` so
    §5.1's delete-vs-retry branch can key on it."""
    provider = await _make_provider(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]

    body = (
        b'{"error":"invalid_grant",'
        b'"error_description":"refresh token not found"}'
    )
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://auth.example.invalid/token"),
        content=body,
    )
    before = datetime.now(UTC)
    ok = await provider._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]
    after = datetime.now(UTC)

    assert ok is False  # SDK contract: non-200 → False
    sig = provider.last_refresh_failure
    assert sig is not None
    assert sig.status_code == 400
    assert sig.error_code == "invalid_grant"
    assert "invalid_grant" in sig.body_excerpt
    assert before <= sig.timestamp <= after


@pytest.mark.asyncio
async def test_handle_refresh_response_500_captures_signature_without_error_code(
    tmp_path: Path,
) -> None:
    """A 5xx from the token endpoint is the *transient* case §5.1
    should retry. Its signature must carry status=500 and
    ``error_code=None`` — a downstream policy that reads ``error_code``
    to decide "delete" must not treat a 500 as ``invalid_grant``."""
    provider = await _make_provider(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]

    body = b"<html>502 Bad Gateway</html>"
    response = httpx.Response(
        500,
        request=httpx.Request("POST", "https://auth.example.invalid/token"),
        content=body,
    )
    ok = await provider._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]

    assert ok is False
    sig = provider.last_refresh_failure
    assert sig is not None
    assert sig.status_code == 500
    assert sig.error_code is None
    assert "502 Bad Gateway" in sig.body_excerpt


@pytest.mark.asyncio
async def test_handle_refresh_response_truncates_large_body(
    tmp_path: Path,
) -> None:
    """An upstream that returns a megabyte of HTML on failure should
    not pin that megabyte in memory forever on every provider
    instance. The excerpt is capped at ``REFRESH_FAILURE_BODY_LIMIT``
    bytes."""
    provider = await _make_provider(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]

    body = b"x" * (REFRESH_FAILURE_BODY_LIMIT * 4)
    response = httpx.Response(
        500,
        request=httpx.Request("POST", "https://auth.example.invalid/token"),
        content=body,
    )
    await provider._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]

    sig = provider.last_refresh_failure
    assert sig is not None
    assert len(sig.body_excerpt) <= REFRESH_FAILURE_BODY_LIMIT


@pytest.mark.asyncio
async def test_handle_refresh_response_200_does_not_set_signature(
    tmp_path: Path,
) -> None:
    """A successful refresh must leave ``last_refresh_failure`` alone.
    If this ever flips to "set on any response", every successful
    cycle would look like a failure and §5.1 would delete tokens on
    the happy path."""
    provider = await _make_provider(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]

    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://auth.example.invalid/token"),
        json={
            "access_token": "new-at",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "new-rt",
        },
    )
    ok = await provider._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]

    assert ok is True
    assert provider.last_refresh_failure is None


@pytest.mark.asyncio
async def test_handle_refresh_response_undecodable_body_captures_status(
    tmp_path: Path,
) -> None:
    """Some upstreams return binary blobs or non-UTF-8 encodings on
    error. The signature must still be captured — the status code is
    the load-bearing bit for retry/delete policy — and the excerpt
    should fall back cleanly rather than raise."""
    provider = await _make_provider(tmp_path)
    await provider._initialize()  # pyright: ignore[reportPrivateUsage]

    body = bytes([0xff, 0xfe, 0xfd, 0x00, 0x01])
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://auth.example.invalid/token"),
        content=body,
    )
    ok = await provider._handle_refresh_response(response)  # pyright: ignore[reportPrivateUsage]

    assert ok is False
    sig = provider.last_refresh_failure
    assert sig is not None
    assert sig.status_code == 400


# ── Integration: drive async_auth_flow past a failing refresh ────────


@pytest.mark.asyncio
async def test_async_auth_flow_failing_refresh_populates_signature(
    tmp_path: Path,
) -> None:
    """Load-bearing integration test: start with an expired stored
    token (so the SDK attempts refresh on the first yield), feed the
    generator a 400 ``invalid_grant`` response, and assert the
    provider's ``last_refresh_failure`` is populated before any
    authorization_code / ``_noop_callback`` path gets a chance to
    run. Drives the real SDK generator rather than a hand-built
    context — that's the pattern §8 of the durability doc warns
    against bypassing."""
    provider = await _make_provider(tmp_path)

    original = httpx.Request("GET", UPSTREAM_URL)
    flow = provider.async_auth_flow(original)
    refresh_req = await flow.__anext__()
    assert b"grant_type=refresh_token" in refresh_req.read()

    refresh_fail = httpx.Response(
        400,
        request=refresh_req,
        content=b'{"error":"invalid_grant"}',
    )
    # Second yield will be the original request again with no bearer
    # (context tokens cleared by the SDK's default handler). We don't
    # exercise it further — the signature must already be captured
    # by the time the refresh response is processed.
    await flow.asend(refresh_fail)

    sig = provider.last_refresh_failure
    assert sig is not None
    assert sig.status_code == 400
    assert sig.error_code == "invalid_grant"


# ── RefreshFailureSignature serialization ───────────────────────────


def test_signature_to_dict_round_trips_through_json() -> None:
    """The signature rides into Mongo via ``set_connection_error``'s
    ``signature`` kwarg as a plain dict. Keep the dict shape stable
    (same field names) so operators running raw ``db.connections.find``
    queries can read it without a schema consult."""
    sig = RefreshFailureSignature(
        status_code=400,
        body_excerpt='{"error":"invalid_grant"}',
        error_code="invalid_grant",
        timestamp=datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
    )
    d = sig.to_dict()
    assert d["status_code"] == 400
    assert d["error_code"] == "invalid_grant"
    assert d["body_excerpt"] == '{"error":"invalid_grant"}'
    assert d["timestamp"] == "2026-04-24T12:00:00+00:00"
