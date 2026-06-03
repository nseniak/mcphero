"""Tests for PendingAuthCoordinator with signed state tokens."""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from mcpolis.adapters.auth.hmac_token import verify_token
from mcpolis.adapters.auth.pending_auth import (
    STATE_TOKEN_MAX_AGE,
    PendingAuthCoordinator,
)


def make_signing_key() -> bytes:
    return hashlib.sha256(b"test-secret").digest()


@pytest.mark.asyncio
async def test_redirect_handler_replaces_state_with_signed_token() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    await pending.redirect_handler(
        "https://github.com/login/oauth/authorize?state=abc123&scope=repo"
    )

    assert pending.redirect_url is not None
    assert pending.auth_state == "abc123"

    # The URL should contain a signed state, not the original
    parsed = urlparse(pending.redirect_url)
    params = parse_qs(parsed.query)
    signed_state = params["state"][0]

    # Verify the signed token
    payload = verify_token(signed_state, key, max_age=STATE_TOKEN_MAX_AGE)
    assert payload is not None
    assert payload["org"] == "acme"
    assert payload["uid"] == "github"
    assert payload["usr"] == "alice"
    assert payload["ost"] == "abc123"

    # scope should still be there
    assert params["scope"] == ["repo"]


@pytest.mark.asyncio
async def test_complete_by_key_signals_callback_handler() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    await pending.redirect_handler(
        "https://example.com/auth?state=xyz"
    )

    async def complete_later() -> None:
        await asyncio.sleep(0.01)
        coordinator.complete_by_key(
            "acme", "github", "alice", "the-code", "xyz"
        )

    asyncio.create_task(complete_later())

    code, state = await pending.callback_handler()
    assert code == "the-code"
    assert state == "xyz"


@pytest.mark.asyncio
async def test_wait_for_redirect_url() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "slack", "bob")

    async def redirect_later() -> None:
        await asyncio.sleep(0.01)
        await pending.redirect_handler(
            "https://slack.com/oauth?state=s1"
        )

    asyncio.create_task(redirect_later())

    url = await pending.wait_for_redirect_or_refresh()
    assert url is not None
    assert "slack.com/oauth" in url


@pytest.mark.asyncio
async def test_complete_by_key_unknown_returns_none() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    result = coordinator.complete_by_key(
        "acme", "unknown", "user", "code", "state"
    )
    assert result is None


@pytest.mark.asyncio
async def test_create_pending_replaces_existing() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)

    pending1 = coordinator.create_pending("acme", "github", "alice")
    pending2 = coordinator.create_pending("acme", "github", "alice")
    assert pending2 is not pending1

    # Old pending should be gone
    result = coordinator.complete_by_key(
        "acme", "github", "alice", "code", "st"
    )
    assert result is pending2


@pytest.mark.asyncio
async def test_cleanup_removes_pending() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    coordinator.create_pending("acme", "github", "alice")

    coordinator.cleanup("acme", "github", "alice")

    assert coordinator.get_pending("acme", "github", "alice") is None
    assert coordinator.complete_by_key(
        "acme", "github", "alice", "code", "st"
    ) is None


@pytest.mark.asyncio
async def test_pre_filled_code_completes_callback_immediately() -> None:
    """Simulates the restart recovery path: code is pre-filled before
    the background task starts, so callback_handler returns instantly."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    # Pre-fill the code (as initiate_oauth_connection does after restart)
    pending.complete("stored-code", "original-state")

    # callback_handler should return immediately
    code, state = await asyncio.wait_for(
        pending.callback_handler(), timeout=1.0
    )
    assert code == "stored-code"
    assert state == "original-state"


@pytest.mark.asyncio
async def test_same_user_same_upstream_different_orgs_do_not_collide() -> None:
    """Superadmin who kicks off OAuth for the same upstream in two orgs
    in parallel must see both PendingAuths preserved, keyed by org."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)

    pending_a = coordinator.create_pending("acme", "github", "__admin__")
    pending_b = coordinator.create_pending("beta", "github", "__admin__")
    assert pending_a is not pending_b

    # Completing one does not consume the other
    result_a = coordinator.complete_by_key(
        "acme", "github", "__admin__", "code-a", "st-a"
    )
    assert result_a is pending_a

    # Beta's pending is still there
    assert coordinator.get_pending(
        "beta", "github", "__admin__"
    ) is pending_b

    result_b = coordinator.complete_by_key(
        "beta", "github", "__admin__", "code-b", "st-b"
    )
    assert result_b is pending_b


@pytest.mark.asyncio
async def test_signed_state_token_carries_org_for_callback() -> None:
    """Callback route reads the org from the signed state token, not
    from the session cookie. Make sure the token actually carries it."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "mixpanel", "__admin__")

    await pending.redirect_handler(
        "https://mixpanel.com/oauth/authorize?state=orig"
    )

    assert pending.redirect_url is not None
    signed_state = parse_qs(urlparse(pending.redirect_url).query)["state"][0]
    payload = verify_token(signed_state, key, max_age=STATE_TOKEN_MAX_AGE)
    assert payload is not None
    assert payload["org"] == "acme"
