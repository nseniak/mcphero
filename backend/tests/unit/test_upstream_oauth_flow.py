"""Tests for the upstream OAuth connection flow.

Covers:
- try_connect_with_stored_tokens (token existence, expiry, connection success/failure)
- initiate_oauth_connection (stored tokens path vs new OAuth flow path)
- on_tokens_acquired callback (SSE notification after background token acquisition)
- EventBus broadcast behavior
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.event_stream_inprocess import InProcessEventStream
from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.model.events import Event
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import TransportType
from mcpolis.domain.services.upstream_connection_service import (
    connect_and_refresh_tools,
    initiate_oauth_connection,
    try_connect_with_stored_tokens,
)
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.model.upstream import UpstreamDefinition
from tests.unit.factories import make_upstream_auth, make_upstream_definition


SERVER_URL = "http://localhost:8080"


def make_signing_key() -> bytes:
    return hashlib.sha256(b"test-secret").digest()


def make_http_upstream(
    id: str = "mixpanel",
    auth_mode: AuthMode = AuthMode.per_user_oauth,
) -> UpstreamDefinition:
    return make_upstream_definition(
        id=id,
        transport=TransportType.streamable_http,
        url="http://localhost:9999/mcp",
        auth=make_upstream_auth(mode=auth_mode),
    )


def make_oauth_token(
    expires_at: datetime | None = None,
) -> InternalOAuthToken:
    return InternalOAuthToken(
        access_token="access-123",
        refresh_token="refresh-456",
        expires_at=expires_at,
        scopes=["read"],
    )


def make_client_manager() -> MagicMock:
    cm = MagicMock()
    cm.connect_upstream_for_user = AsyncMock()
    return cm


# ── try_connect_with_stored_tokens ──────────────────────────────────


@pytest.mark.asyncio
async def test_try_connect_no_tokens(tmp_path: Path) -> None:
    """Returns None when no tokens are stored."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is None
    cm.connect_upstream_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_try_connect_with_valid_tokens(tmp_path: Path) -> None:
    """Connects successfully when valid (non-expired) tokens exist."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()

    token = make_oauth_token(expires_at=datetime.now(UTC) + timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is not None
    assert result.connected is True
    cm.connect_upstream_for_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_connect_with_no_expiry_tokens(tmp_path: Path) -> None:
    """Assumes tokens are usable when expires_at is None."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()

    token = make_oauth_token(expires_at=None)
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is not None
    assert result.connected is True
    cm.connect_upstream_for_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_connect_with_expired_tokens(tmp_path: Path) -> None:
    """Returns None when tokens are expired."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()

    token = make_oauth_token(expires_at=datetime.now(UTC) - timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is None
    cm.connect_upstream_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_try_connect_connection_failure(tmp_path: Path) -> None:
    """Returns None when tokens exist but connection fails."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    cm.connect_upstream_for_user.side_effect = RuntimeError("connection refused")

    token = make_oauth_token(expires_at=datetime.now(UTC) + timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is None


@pytest.mark.asyncio
async def test_try_connect_stdio_upstream(tmp_path: Path) -> None:
    """Returns None for stdio upstreams (no HTTP, no OAuth)."""
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream_definition(
        id="local", transport=TransportType.stdio,
    )
    cm = make_client_manager()

    result = await try_connect_with_stored_tokens(
        DEFAULT_ORG_ID, upstream, "__admin__", store, cm, SERVER_URL,
    )
    assert result is None


# ── initiate_oauth_connection ───────────────────────────────────────


@pytest.mark.asyncio
async def test_initiate_uses_stored_tokens_first(tmp_path: Path) -> None:
    """When valid tokens exist, connects directly without creating PendingAuth."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    token = make_oauth_token(expires_at=datetime.now(UTC) + timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    result = await initiate_oauth_connection(
        DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
    )
    assert result.connected is True
    assert result.authorization_url is None
    # Should NOT have created a PendingAuth (used try_connect_with_stored_tokens)
    assert coordinator.get_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__") is None


@pytest.mark.asyncio
async def test_initiate_no_tokens_starts_oauth_flow(tmp_path: Path) -> None:
    """When no tokens exist, returns an authorization URL."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    # Mock the background task — it would normally make an HTTP request
    # to the upstream to trigger the OAuth redirect.
    # _start_background_token_acquisition is a sync function that returns a Task.
    # We need the redirect_handler to be called asynchronously so that
    # wait_for_redirect_or_refresh() can observe it.
    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._start_background_token_acquisition",
    ) as mock_bg:
        def fake_start(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
            pending = _args[3]  # 4th positional arg
            async def _bg() -> None:
                await pending.redirect_handler(
                    "https://auth.example.com/authorize?state=original"
                )
            return asyncio.create_task(_bg())

        mock_bg.side_effect = fake_start

        result = await initiate_oauth_connection(
            DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
        )

    assert result.connected is False
    assert result.authorization_url is not None
    assert "auth.example.com" in result.authorization_url


@pytest.mark.asyncio
async def test_initiate_with_stored_code_from_restart(tmp_path: Path) -> None:
    """When a pending code exists on disk (server restarted during OAuth),
    it should be pre-filled into the PendingAuth."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    # Store a pending code as if the callback arrived during downtime
    await store.put_pending_code(DEFAULT_ORG_ID, "mixpanel", "__admin__", "the-code", "orig-state")

    # Also store tokens so try_connect_with_stored_tokens fails first
    # (expired tokens force falling through to the OAuth path)
    token = make_oauth_token(expires_at=datetime.now(UTC) - timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._start_background_token_acquisition",
    ) as mock_bg:
        # The background task should receive a pending with pre-filled code.
        # It will call callback_handler which should return immediately.
        def fake_start(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
            pending = _args[3]  # 4th positional arg
            # Verify the code was pre-filled
            assert pending.auth_code == "the-code"
            assert pending.auth_state == "orig-state"

            async def _bg() -> None:
                pending.mark_tokens_refreshed()
            return asyncio.create_task(_bg())

        mock_bg.side_effect = fake_start

        result = await initiate_oauth_connection(
            DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
        )

    # Token refresh path → should try to connect
    # (connect_upstream_for_user is called, which is our mock)
    assert result.connected is True or result.error is not None

    # Verify the pending code was consumed
    remaining = await store.pop_pending_code(DEFAULT_ORG_ID,"mixpanel", "__admin__")
    assert remaining is None


@pytest.mark.asyncio
async def test_initiate_does_not_clobber_pending_on_retry(
    tmp_path: Path,
) -> None:
    """Calling initiate_oauth_connection again after tokens are stored
    should NOT create a new PendingAuth — it should use stored tokens."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    # Simulate: first call started OAuth, tokens were acquired
    token = make_oauth_token(expires_at=datetime.now(UTC) + timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    # Second call (the retry after SSE notification) — should use tokens
    result = await initiate_oauth_connection(
        DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
    )
    assert result.connected is True
    # No PendingAuth should have been created
    assert coordinator.get_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__") is None


# ── on_tokens_acquired callback ─────────────────────────────────────


@pytest.mark.asyncio
async def test_on_tokens_acquired_called_after_background_acquisition(
    tmp_path: Path,
) -> None:
    """The on_tokens_acquired callback fires after the background task
    stores tokens."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        _start_background_token_acquisition,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    coordinator = PendingAuthCoordinator(make_signing_key())
    pending = coordinator.create_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__")

    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    callback_called = False

    def on_acquired() -> None:
        nonlocal callback_called
        callback_called = True

    # Pre-store tokens so the check at the end of _acquire_tokens finds them
    from mcp.shared.auth import OAuthToken as SdkToken
    await storage.set_tokens(SdkToken(
        access_token="new-access",
        token_type="Bearer",
    ))

    # Mock httpx to avoid real HTTP requests
    with patch("mcpolis.domain.services.upstream_connection_service.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)

        task = _start_background_token_acquisition(
            upstream, MagicMock(), storage, pending,
            on_tokens_acquired=on_acquired,
        )
        await asyncio.wait_for(task, timeout=5.0)

    assert callback_called


@pytest.mark.asyncio
async def test_on_tokens_acquired_not_called_when_no_tokens(
    tmp_path: Path,
) -> None:
    """The callback should NOT fire if no tokens were acquired."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        _start_background_token_acquisition,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    coordinator = PendingAuthCoordinator(make_signing_key())
    pending = coordinator.create_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__")

    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    callback_called = False

    def on_acquired() -> None:
        nonlocal callback_called
        callback_called = True

    # No tokens stored — callback should not fire
    with patch("mcpolis.domain.services.upstream_connection_service.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)

        task = _start_background_token_acquisition(
            upstream, MagicMock(), storage, pending,
            on_tokens_acquired=on_acquired,
        )
        await asyncio.wait_for(task, timeout=5.0)

    assert not callback_called


@pytest.mark.asyncio
async def test_on_error_called_when_no_tokens_acquired(
    tmp_path: Path,
) -> None:
    """on_error fires when the background task finishes without storing tokens."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthFailureReason,
        _start_background_token_acquisition,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    coordinator = PendingAuthCoordinator(make_signing_key())
    pending = coordinator.create_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__")

    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    error_calls: list[tuple[str, OAuthFailureReason]] = []

    def on_error(msg: str, reason: OAuthFailureReason) -> None:
        error_calls.append((msg, reason))

    # No tokens stored — on_error should fire
    with patch("mcpolis.domain.services.upstream_connection_service.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)

        task = _start_background_token_acquisition(
            upstream, MagicMock(), storage, pending,
            on_error=on_error,
        )
        await asyncio.wait_for(task, timeout=5.0)

    assert len(error_calls) == 1
    assert error_calls[0][1] == OAuthFailureReason.token_exchange


@pytest.mark.asyncio
async def test_on_error_not_called_when_tokens_acquired(
    tmp_path: Path,
) -> None:
    """on_error should NOT fire when tokens were successfully acquired."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthFailureReason,
        _start_background_token_acquisition,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    coordinator = PendingAuthCoordinator(make_signing_key())
    pending = coordinator.create_pending(DEFAULT_ORG_ID, "mixpanel", "__admin__")

    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    error_calls: list[tuple[str, OAuthFailureReason]] = []

    def on_error(msg: str, reason: OAuthFailureReason) -> None:
        error_calls.append((msg, reason))

    # Pre-store tokens
    from mcp.shared.auth import OAuthToken as SdkToken
    await storage.set_tokens(SdkToken(
        access_token="new-access",
        token_type="Bearer",
    ))

    with patch("mcpolis.domain.services.upstream_connection_service.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)

        task = _start_background_token_acquisition(
            upstream, MagicMock(), storage, pending,
            on_error=on_error,
        )
        await asyncio.wait_for(task, timeout=5.0)

    assert len(error_calls) == 0


# ── EventBus broadcast ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_bus_broadcast_reaches_all_subscribers() -> None:
    """Events with user_email=None should reach all subscribers."""
    bus = InProcessEventStream()

    received_alice: list[Event] = []
    received_bob: list[Event] = []

    async def collect(email: str, target: list[Event]) -> None:
        async for event in bus.subscribe(DEFAULT_ORG_ID, email):
            if event is not None:
                target.append(event)
                break  # Just collect one

    task_alice = asyncio.create_task(collect("alice@test.com", received_alice))
    task_bob = asyncio.create_task(collect("bob@test.com", received_bob))

    # Let subscribers register
    await asyncio.sleep(0.01)

    bus.publish(DEFAULT_ORG_ID, Event(
        type="upstream_tokens_acquired",
        user_email=None,
        payload={"upstream_id": "mixpanel"},
    ))

    await asyncio.wait_for(
        asyncio.gather(task_alice, task_bob), timeout=2.0,
    )

    assert len(received_alice) == 1
    assert received_alice[0].type == "upstream_tokens_acquired"
    assert len(received_bob) == 1
    assert received_bob[0].type == "upstream_tokens_acquired"


@pytest.mark.asyncio
async def test_event_bus_user_specific_does_not_leak() -> None:
    """Events with a specific user_email should not reach other users."""
    bus = InProcessEventStream()

    received_alice: list[Event] = []
    received_bob: list[Event] = []

    async def collect(email: str, target: list[Event]) -> None:
        async for event in bus.subscribe(DEFAULT_ORG_ID, email):
            if event is not None:
                target.append(event)
                break

    task_alice = asyncio.create_task(collect("alice@test.com", received_alice))
    task_bob = asyncio.create_task(collect("bob@test.com", received_bob))

    await asyncio.sleep(0.01)

    bus.publish(DEFAULT_ORG_ID, Event(
        type="upstream_tokens_acquired",
        user_email="alice@test.com",
        payload={"upstream_id": "mixpanel"},
    ))

    # Alice should receive it
    await asyncio.wait_for(task_alice, timeout=2.0)
    assert len(received_alice) == 1

    # Bob should NOT have received it — cancel his task
    await asyncio.sleep(0.05)
    assert len(received_bob) == 0
    task_bob.cancel()


# ── connect_and_refresh_tools ───────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_and_refresh_tools_passes_callback(
    tmp_path: Path,
) -> None:
    """connect_and_refresh_tools passes on_tokens_acquired through and
    schedules the catalog refresh as a background task so slow
    upstreams don't block the connect response.
    """
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())
    tool_registry = MagicMock(spec=ToolRegistry)
    tool_registry.refresh_upstream = AsyncMock()
    # Pretend the pill has been on screen long enough so the min-display
    # sleep in `_refresh_upstream_in_background` is a no-op for the test.
    tool_registry.refreshing_started_at = MagicMock(return_value=0.0)

    # Store valid tokens so it connects immediately
    token = make_oauth_token(expires_at=datetime.now(UTC) + timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID,"__admin__", "mixpanel", token)

    callback_called = False
    refreshed_callback_called = False

    def on_acquired() -> None:
        nonlocal callback_called
        callback_called = True

    def on_refreshed() -> None:
        nonlocal refreshed_callback_called
        refreshed_callback_called = True

    result = await connect_and_refresh_tools(
        DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm,
        tool_registry, SERVER_URL,
        on_tokens_acquired=on_acquired,
        on_tools_refreshed=on_refreshed,
    )
    assert result.connected is True
    # refresh_upstream is now scheduled as a background task — yield
    # the loop so it gets a chance to run before we assert.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    tool_registry.refresh_upstream.assert_awaited_once_with("mixpanel")
    assert refreshed_callback_called
    # Callback is NOT called here (tokens were already stored, no
    # browser-redirect path)
    assert not callback_called


# ── client_info redirect_uri self-heal ──────────────────────────────


@pytest.mark.asyncio
async def test_build_oauth_provider_keeps_stale_client_info_on_silent_path(
    tmp_path: Path,
) -> None:
    """Silent paths (refresh / liveness probe / reconnect) MUST NOT drop
    stored DCR client_info on a callback-URL change.

    ``grant_type=refresh_token`` doesn't carry ``redirect_uri`` (RFC 6749
    §6), so a callback-URL change is invisible to the upstream's token
    endpoint. Dropping the client_info here would force a fresh DCR with
    a new ``client_id``; the next refresh would then run against that
    new ``client_id`` while the stored ``refresh_token`` was issued under
    the old one — upstream rejects with ``invalid_grant: Client ID
    mismatch``. That's the 2026-05-07 mcpolis.seniak.com → mcphero.io
    rebrand incident."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        _build_oauth_provider,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    # Seed a stale client_info whose redirect_uris point at the old domain.
    await store.put_client_info(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
        {
            "client_id": "stale-client-id",
            "client_secret": "stale-secret",
            "redirect_uris": ["http://localhost:8080/oauth/upstream/callback"],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    async def noop_redirect(_url: str) -> None: ...
    async def noop_callback() -> tuple[str, str | None]:
        return ("", None)

    await _build_oauth_provider(
        upstream, storage, noop_redirect, noop_callback, SERVER_URL,
    )

    # The silent path must preserve the stored client_info untouched —
    # the still-valid refresh_token depends on this client_id.
    info = await storage.get_client_info()
    assert info is not None
    assert info.client_id == "stale-client-id"


@pytest.mark.asyncio
async def test_consent_path_drops_client_info_on_stale_redirect_uri(
    tmp_path: Path,
) -> None:
    """The fresh-consent path is the one place where a callback-URL
    change MUST drop the stored client_info — the upstream's authorize
    endpoint rejects redirect_uris that don't match what it has on file
    for the registered client. ``_drop_client_info_if_redirect_stale``
    exists to be called from there (and only there)."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        _drop_client_info_if_redirect_stale,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    await store.put_client_info(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
        {
            "client_id": "stale-client-id",
            "client_secret": "stale-secret",
            "redirect_uris": ["http://localhost:8080/oauth/upstream/callback"],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    await _drop_client_info_if_redirect_stale(storage, upstream, SERVER_URL)

    assert await storage.get_client_info() is None


@pytest.mark.asyncio
async def test_consent_path_keeps_client_info_when_redirect_uri_matches(
    tmp_path: Path,
) -> None:
    """No-op when the stored redirect_uri already matches the current
    callback — common case for established connections that haven't
    seen a callback-URL change."""
    from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
    from mcpolis.domain.services.upstream_connection_service import (
        _drop_client_info_if_redirect_stale,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    current_callback = f"{SERVER_URL}/api/oauth/upstream/callback"
    await store.put_client_info(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
        {
            "client_id": "fresh-client-id",
            "client_secret": "fresh-secret",
            "redirect_uris": [current_callback],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    storage = McpTokenStorage(store, DEFAULT_ORG_ID, "mixpanel", "__admin__")

    await _drop_client_info_if_redirect_stale(storage, upstream, SERVER_URL)

    info = await storage.get_client_info()
    assert info is not None
    assert info.client_id == "fresh-client-id"


# ── Edge branches in ``initiate_oauth_connection`` ──────────────────


@pytest.mark.asyncio
async def test_initiate_finalize_silent_refresh_failure_surfaces_post_refresh_error(
    tmp_path: Path,
) -> None:
    """When the background task signals a silent token refresh
    (``auth_url is None``) but the subsequent
    ``connect_upstream_for_user`` raises, the error message must
    distinguish "auth itself failed" from "auth succeeded but the
    post-auth connect failed". Operators triage these differently:
    the former is a user-credential issue, the latter is an upstream
    transport issue. The user-facing string starts with
    "Authentication succeeded but the connection ... failed" and is
    operator-grep contract."""
    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    cm.connect_upstream_for_user = AsyncMock(
        side_effect=RuntimeError("transport blew up after refresh"),
    )
    coordinator = PendingAuthCoordinator(make_signing_key())

    # Seed an expired token so the fast path falls through to the
    # background-acquisition branch.
    expired = make_oauth_token(expires_at=datetime.now(UTC) - timedelta(hours=1))
    await store.put_user_token(DEFAULT_ORG_ID, "__admin__", "mixpanel", expired)

    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._start_background_token_acquisition",
    ) as mock_bg:
        def fake_start(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
            pending = _args[3]
            async def _bg() -> None:
                pending.mark_tokens_refreshed()
            return asyncio.create_task(_bg())

        mock_bg.side_effect = fake_start

        result = await initiate_oauth_connection(
            DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
        )

    assert result.connected is False
    assert result.error is not None
    assert result.error.startswith(
        "Authentication succeeded but the connection to this MCP failed:"
    )
    # Pending slot must be cleaned up so the next attempt starts fresh.
    assert coordinator.get_pending(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
    ) is None


@pytest.mark.asyncio
async def test_initiate_timeout_returns_discovery_failure(
    tmp_path: Path,
) -> None:
    """If neither a redirect URL, a silent refresh, nor a hard failure
    arrives within the deadline, the connect endpoint must return a
    ``discovery``-classified error so the dialog tells the user the
    server is unreachable rather than spinning indefinitely."""
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthFailureReason,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._start_background_token_acquisition",
    ) as mock_bg:
        def fake_start(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
            async def _bg() -> None: ...
            return asyncio.create_task(_bg())

        mock_bg.side_effect = fake_start

        with patch(
            "mcpolis.adapters.auth.pending_auth.PendingAuth"
            ".wait_for_redirect_or_refresh",
            side_effect=TimeoutError("deadline reached"),
        ):
            result = await initiate_oauth_connection(
                DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
            )

    assert result.connected is False
    assert result.failure_reason is OAuthFailureReason.discovery
    assert result.error is not None
    assert "Could not reach this MCP server" in result.error
    assert coordinator.get_pending(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
    ) is None


@pytest.mark.asyncio
async def test_initiate_unreachable_returns_upstream_unavailable(
    tmp_path: Path,
) -> None:
    """The background task can short-circuit the 30s discovery wait
    by raising ``UpstreamUnreachableError`` with a user-facing message.
    initiate_oauth_connection must surface that message verbatim and
    classify it as ``upstream_unavailable`` so the dialog copy is
    accurate ("The server is down" rather than "We couldn't reach
    it")."""
    from mcpolis.adapters.auth.pending_auth import UpstreamUnreachableError
    from mcpolis.domain.services.upstream_connection_service import (
        OAuthFailureReason,
    )

    store = FileConnectionStore(tmp_path)
    upstream = make_http_upstream()
    cm = make_client_manager()
    coordinator = PendingAuthCoordinator(make_signing_key())

    with patch(
        "mcpolis.domain.services.upstream_connection_service"
        "._start_background_token_acquisition",
    ) as mock_bg:
        def fake_start(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
            async def _bg() -> None: ...
            return asyncio.create_task(_bg())

        mock_bg.side_effect = fake_start

        with patch(
            "mcpolis.adapters.auth.pending_auth.PendingAuth"
            ".wait_for_redirect_or_refresh",
            side_effect=UpstreamUnreachableError(
                "DNS lookup failed for upstream",
            ),
        ):
            result = await initiate_oauth_connection(
                DEFAULT_ORG_ID, upstream, "__admin__", store, coordinator, cm, SERVER_URL,
            )

    assert result.connected is False
    assert result.failure_reason is OAuthFailureReason.upstream_unavailable
    assert result.error == "DNS lookup failed for upstream"
    assert coordinator.get_pending(
        DEFAULT_ORG_ID, "mixpanel", "__admin__",
    ) is None
