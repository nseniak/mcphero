"""Tests for McpTokenStorage adapter."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mcpolis.adapters.auth.mcp_token_storage import (  # pyright: ignore[reportPrivateUsage]
    McpTokenStorage,
    _internal_to_sdk_token,
)
from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID


def _make_internal_token(*, expired: bool) -> InternalOAuthToken:
    delta = timedelta(minutes=-5 if expired else 30)
    return InternalOAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + delta,
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_set_and_get_tokens(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")

    sdk_token = OAuthToken(
        access_token="access-123",
        token_type="Bearer",
        expires_in=3600,
        scope="read write",
        refresh_token="refresh-456",
    )

    await storage.set_tokens(sdk_token)
    result = await storage.get_tokens()

    assert result is not None
    assert result.access_token == "access-123"
    assert result.refresh_token == "refresh-456"
    assert result.scope == "read write"


@pytest.mark.asyncio
async def test_get_tokens_returns_none_when_empty(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")

    result = await storage.get_tokens()
    assert result is None


@pytest.mark.asyncio
async def test_set_tokens_emits_rotation_event(
    tmp_path: Path,
) -> None:
    """Every rotation flows through ``set_tokens``; this is the one
    event that catches inline SDK-driven rotations (which don't hit
    the periodic loop's ``oauth.token.refresh.started/success``
    pair). Asserts the event name + the ``(org, upstream, user)``
    fields so an operator can correlate to the user that rotated."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "notion", "alice@co.com")

    sdk_token = OAuthToken(
        access_token="access-NEW123",
        token_type="Bearer",
        expires_in=3600,
        refresh_token="refresh-NEW456",
    )

    with structlog.testing.capture_logs() as logs:
        await storage.set_tokens(sdk_token)

    rotations = [
        e for e in logs
        if e.get("event") == "oauth.token.storage.rotated"
    ]
    assert len(rotations) == 1, (
        f"expected one rotation event, got: {logs}"
    )
    record = rotations[0]
    assert record.get("upstream_id") == "notion"
    assert record.get("user") == "alice@co.com"
    assert record.get("org_id") == DEFAULT_ORG_ID
    assert record.get("expires_in_seconds") == 3600
    assert record.get("new_access_suffix") == "NEW123"
    # First rotation — no previous token to compare against.
    assert record.get("previous_access_suffix") is None


@pytest.mark.asyncio
async def test_set_tokens_records_previous_access_suffix_on_replacement(
    tmp_path: Path,
) -> None:
    """When a token already exists, the rotation event carries both
    the previous and new access-token suffixes. The 6-char suffix
    is enough to visually confirm the value actually changed (vs a
    no-op re-write) without logging full credentials."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "notion", "alice@co.com")

    await storage.set_tokens(OAuthToken(
        access_token="access-OLDxyz",
        token_type="Bearer",
        expires_in=3600,
        refresh_token="refresh-OLD",
    ))

    with structlog.testing.capture_logs() as logs:
        await storage.set_tokens(OAuthToken(
            access_token="access-NEWabc",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="refresh-NEW",
        ))

    rotations = [
        e for e in logs
        if e.get("event") == "oauth.token.storage.rotated"
    ]
    assert len(rotations) == 1
    record = rotations[0]
    assert record.get("previous_access_suffix") == "OLDxyz"
    assert record.get("new_access_suffix") == "NEWabc"


@pytest.mark.asyncio
async def test_set_and_get_client_info(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")

    client_info = OAuthClientInformationFull(
        client_id="dyn-client-id",
        client_secret="dyn-client-secret",
        redirect_uris=[AnyUrl("http://localhost:8000/callback")],
    )

    await storage.set_client_info(client_info)
    result = await storage.get_client_info()

    assert result is not None
    assert result.client_id == "dyn-client-id"
    assert result.client_secret == "dyn-client-secret"


@pytest.mark.asyncio
async def test_get_client_info_returns_none_when_empty(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")

    result = await storage.get_client_info()
    assert result is None


@pytest.mark.asyncio
async def test_separate_users_have_separate_storage(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    storage_alice = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")
    storage_bob = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "bob")

    await storage_alice.set_tokens(OAuthToken(
        access_token="alice-token",
        token_type="Bearer",
    ))
    await storage_bob.set_tokens(OAuthToken(
        access_token="bob-token",
        token_type="Bearer",
    ))

    alice_result = await storage_alice.get_tokens()
    bob_result = await storage_bob.get_tokens()

    assert alice_result is not None
    assert alice_result.access_token == "alice-token"
    assert bob_result is not None
    assert bob_result.access_token == "bob-token"


# ── expires_in round-trip ─────────────────────────────────────────
# ``_internal_to_sdk_token`` must convert the stored absolute
# ``expires_at`` into the relative ``expires_in`` the SDK expects. The
# subclass in ``upstream_connection_service`` then feeds that value into
# ``OAuthContext.update_token_expiry`` after ``_initialize`` so
# ``is_token_valid()`` can tell an expired stored token apart from a
# live one. The end-to-end behavior is pinned in
# ``test_upstream_oauth_silent_refresh.py``.


def test_expired_internal_token_produces_negative_expires_in() -> None:
    sdk_token = _internal_to_sdk_token(_make_internal_token(expired=True))
    assert sdk_token.expires_in is not None
    assert sdk_token.expires_in < 0


def test_live_internal_token_produces_positive_expires_in() -> None:
    sdk_token = _internal_to_sdk_token(_make_internal_token(expired=False))
    assert sdk_token.expires_in is not None
    assert sdk_token.expires_in > 0


# ── refresh_margin_seconds clamping ───────────────────────────────
# The proactive-refresh mechanism that makes our 20-min margin
# actually effective. When the stored token's real expiry is within
# ``refresh_margin_seconds`` of now, ``expires_in`` is clamped
# negative so the SDK's zero-buffer ``is_token_valid()`` returns
# False and the refresh branch runs before the request is sent.
# Without this clamp, the margin is cosmetic — the SDK refreshes
# only reactively at real expiry, which is the exact
# boundary-crossing hazard the margin is supposed to eliminate.


def _make_internal_token_with_remaining(
    seconds: float,
) -> InternalOAuthToken:
    return InternalOAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )


def test_margin_clamps_expires_in_when_within_margin() -> None:
    """15 min remaining, margin 20 min → clamp. The SDK will see a
    negative ``expires_in``, resolve ``token_expiry_time`` to a past
    moment, and take the refresh branch."""
    token = _make_internal_token_with_remaining(seconds=15 * 60)
    sdk_token = _internal_to_sdk_token(
        token, refresh_margin_seconds=20 * 60,
    )
    assert sdk_token.expires_in == -1


def test_margin_does_not_clamp_outside_margin() -> None:
    """30 min remaining, margin 20 min → no clamp. Token still has
    plenty of runway, SDK should accept it as valid and not refresh."""
    token = _make_internal_token_with_remaining(seconds=30 * 60)
    sdk_token = _internal_to_sdk_token(
        token, refresh_margin_seconds=20 * 60,
    )
    assert sdk_token.expires_in is not None
    # Not exactly 1800 because ``datetime.now`` advances during the
    # call; should be within a second or two of the seeded remaining.
    assert 1790 < sdk_token.expires_in <= 1800


def test_margin_zero_preserves_native_sdk_behavior() -> None:
    """Callers that don't opt in (default ``refresh_margin_seconds=0``)
    must see the unclamped, real-``expires_in`` value — the same
    shape ``dd5071b`` introduced before we added the margin layer."""
    token = _make_internal_token_with_remaining(seconds=15 * 60)
    sdk_token = _internal_to_sdk_token(token)  # default margin = 0.0
    assert sdk_token.expires_in is not None
    # Positive, since real remaining is positive.
    assert 0 < sdk_token.expires_in <= 900


def test_margin_clamps_already_expired_token() -> None:
    """Already-past-expiry tokens must still be clamped: the existing
    behavior for expired tokens (from the earlier
    ``test_expired_internal_token_produces_negative_expires_in``
    test) must not regress when a caller opts into the margin."""
    token = _make_internal_token_with_remaining(seconds=-60)
    sdk_token = _internal_to_sdk_token(
        token, refresh_margin_seconds=20 * 60,
    )
    assert sdk_token.expires_in == -1


def test_margin_without_expires_at_still_none() -> None:
    """Token with no ``expires_at`` means the provider didn't tell us
    a lifetime — we pass that through to the SDK unchanged. Clamping
    would be wrong here because the token may genuinely be long-lived,
    and the SDK's ``is_token_valid()`` handles ``token_expiry_time is
    None`` as 'valid'."""
    token = InternalOAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=None,
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )
    sdk_token = _internal_to_sdk_token(
        token, refresh_margin_seconds=20 * 60,
    )
    assert sdk_token.expires_in is None


@pytest.mark.asyncio
async def test_storage_propagates_margin_to_get_tokens(
    tmp_path: Path,
) -> None:
    """``McpTokenStorage`` must thread the margin through to
    ``_internal_to_sdk_token`` so the SDK's first ``get_tokens`` call
    (from ``_initialize``) already sees the clamped value."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, DEFAULT_ORG_ID, "github", "alice",
        refresh_margin_seconds=20 * 60,
    )

    # Seed a token with 15 min remaining (inside margin).
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice", "github",
        _make_internal_token_with_remaining(seconds=15 * 60),
    )

    result = await storage.get_tokens()
    assert result is not None
    assert result.expires_in == -1


@pytest.mark.asyncio
async def test_storage_default_margin_is_zero(tmp_path: Path) -> None:
    """Backward compat: callers that don't pass
    ``refresh_margin_seconds`` must see unclamped behavior."""
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "github", "alice")

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice", "github",
        _make_internal_token_with_remaining(seconds=15 * 60),
    )

    result = await storage.get_tokens()
    assert result is not None
    assert result.expires_in is not None
    assert result.expires_in > 0


# ── max_age_seconds clamping (§3.6 seatbelt) + refresh_token gate ──
# The seatbelt forces a proactive rotation once a stored token's
# ``updated_at`` is older than ``max_age_seconds``, regardless of its
# declared ``expires_at``. A rotation *is* a refresh_token grant, so the
# clamp is gated on the token carrying a refresh_token: without one,
# clamping ``expires_in = -1`` can't rotate anything — it strips the auth
# header, self-inflicts a 401, and our silent ``_noop_callback`` deletes
# a still-valid token (the mee6 case: a 365-day access token with no
# refresh_token, destroyed every 4h).


def _make_internal_token_with_age(
    age_seconds: float,
    *,
    refresh_token: str | None,
) -> InternalOAuthToken:
    """A token with a far-future ``expires_at`` (so the margin clamp
    never fires) and a controllable ``updated_at`` age + refresh_token,
    to isolate the max_age path."""
    now = datetime.now(UTC)
    return InternalOAuthToken(
        access_token="at",
        refresh_token=refresh_token,
        expires_at=now + timedelta(days=365),
        scopes=[],
        refresh_token_created_at=now if refresh_token else None,
        updated_at=now - timedelta(seconds=age_seconds),
    )


def test_max_age_clamps_stale_token_when_refresh_token_present() -> None:
    """Stale by ``updated_at`` (5h old, max_age 4h) with a refresh_token
    → clamp to -1. Preserves the Mixpanel-style seatbelt: the SDK sees an
    invalid token and takes the refresh_token branch."""
    token = _make_internal_token_with_age(
        age_seconds=5 * 60 * 60, refresh_token="rt",
    )
    sdk_token = _internal_to_sdk_token(
        token, max_age_seconds=4 * 60 * 60,
    )
    assert sdk_token.expires_in == -1


def test_max_age_does_not_clamp_when_no_refresh_token() -> None:
    """Same stale token but NO refresh_token → no clamp. There is nothing
    to rotate with, so force-expiring would only self-inflict a 401 and
    get the still-valid (365-day) token deleted. The full declared TTL
    must survive. Regression guard for the mee6 zombie-deletion loop."""
    token = _make_internal_token_with_age(
        age_seconds=5 * 60 * 60, refresh_token=None,
    )
    sdk_token = _internal_to_sdk_token(
        token, max_age_seconds=4 * 60 * 60,
    )
    assert sdk_token.expires_in is not None
    # ~365 days of runway preserved, not clamped to -1.
    assert sdk_token.expires_in > 300 * 24 * 60 * 60


def test_max_age_does_not_clamp_fresh_token() -> None:
    """Fresh token (1h old, under the 4h ceiling) with a refresh_token
    → no clamp. Hot path: don't manufacture needless rotations."""
    token = _make_internal_token_with_age(
        age_seconds=60 * 60, refresh_token="rt",
    )
    sdk_token = _internal_to_sdk_token(
        token, max_age_seconds=4 * 60 * 60,
    )
    assert sdk_token.expires_in is not None
    assert sdk_token.expires_in > 0
