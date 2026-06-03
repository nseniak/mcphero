"""Tests for decrypt-failure tolerance in the token-read paths.

Concrete motivation: commit ``f1a7c55`` rotated the HKDF info label from
``mcphero-encryption-v1`` to ``mcpolis-encryption-v1``, which
invalidated every ciphertext written under the old label. Cloud mode's
``FieldEncryptor`` raises ``cryptography.exceptions.InvalidTag`` on a
key mismatch; without the guards pinned here, that exception bubbles
up through ``reconnect_with_stored_tokens`` as a 500 (the caller in
``tool_router`` has no try/except) and through
``refresh_token_for_user`` as an unhandled task exception in the
periodic loop. Both are messy failure modes for what is fundamentally
"user needs to re-auth."

These tests stand up a minimal ``ConnectionStore`` stub that raises
``InvalidTag`` on the relevant reads and assert the callers degrade
gracefully to ``DisconnectReason.token_refresh_failed`` / a silent
skip, respectively — without wiping the ciphertext (the key, not the
data, is likely the thing that's wrong, and wiping makes the mistake
permanent).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.exceptions import InvalidTag

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_connection_service import (
    DisconnectReason,
    reconnect_with_stored_tokens,
    refresh_token_for_user,
)

UPSTREAM_ID = "notion"
USER_ID = "__admin__"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"


def _make_upstream() -> UpstreamDefinition:
    return UpstreamDefinition(
        id=UPSTREAM_ID,
        display_name="Notion",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=UPSTREAM_URL),
        auth=UpstreamAuthConfig(mode=AuthMode.admin_oauth),
    )


def _make_undecryptable_store(
    *,
    want_expiring_token: bool = True,
) -> AsyncMock:
    """A ``ConnectionStore`` that raises ``InvalidTag`` on every attempt
    to read a user token. Writes (``set_connection_error``,
    ``put_user_token``) are no-ops so we can observe whether the
    production code tried to record disconnect state or wipe the
    ciphertext."""
    store = AsyncMock(spec=ConnectionStore)
    store.get_user_token.side_effect = InvalidTag()
    store.set_connection_error = AsyncMock()
    store.delete_user_token = AsyncMock()
    store.put_user_token = AsyncMock()
    store.get_all_stored_tokens = AsyncMock(
        return_value=[(UPSTREAM_ID, USER_ID)] if want_expiring_token else [],
    )
    store.get_client_info = AsyncMock(return_value=None)
    store.put_client_info = AsyncMock()
    store.delete_client_info = AsyncMock()
    store.pop_pending_code = AsyncMock(return_value=None)
    return store


# ── reconnect_with_stored_tokens ─────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_returns_token_refresh_failed_on_decrypt_error(
) -> None:
    """An ``InvalidTag`` from the connection store must surface as
    ``DisconnectReason.token_refresh_failed``, not propagate as an
    unhandled exception that the router then returns as a 500."""
    store = _make_undecryptable_store()
    client_manager = UpstreamClientManager(upstreams=[])

    result = await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=client_manager,
        server_url=SERVER_URL,
    )

    assert result is DisconnectReason.token_refresh_failed


@pytest.mark.asyncio
async def test_reconnect_records_connection_error_on_decrypt_error() -> None:
    """The UI's ``disconnect_reason`` column is sourced from
    ``get_connection_error``. Writing the error here means the admin
    UI shows "re-authentication needed" instead of a stale "connected"
    state the next time someone opens the page."""
    store = _make_undecryptable_store()
    client_manager = UpstreamClientManager(upstreams=[])

    await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=client_manager,
        server_url=SERVER_URL,
    )

    store.set_connection_error.assert_awaited_once()
    (org, upstream_id, reason) = store.set_connection_error.await_args.args
    assert org == DEFAULT_ORG_ID
    assert upstream_id == UPSTREAM_ID
    assert reason is DisconnectReason.token_refresh_failed


@pytest.mark.asyncio
async def test_reconnect_does_not_wipe_undecryptable_ciphertext() -> None:
    """If the *key* is wrong (not the data), wiping the ciphertext
    makes the misconfiguration permanent. Leave it in place so an ops
    fix to ``MCPOLIS_ENCRYPTION_KEY`` restores every user's state on
    the next read."""
    store = _make_undecryptable_store()
    client_manager = UpstreamClientManager(upstreams=[])

    await reconnect_with_stored_tokens(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        effective_user=USER_ID,
        connection_store=store,
        client_manager=client_manager,
        server_url=SERVER_URL,
    )

    store.delete_user_token.assert_not_awaited()


# ── refresh_token_for_user ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_token_skips_cleanly_on_decrypt_error() -> None:
    """Periodic refresh must not crash the loop when one user's token
    is unreadable. The function returns without writing anything; the
    next tool-call path will surface the ``token_refresh_failed``
    state via ``reconnect_with_stored_tokens``."""
    store = _make_undecryptable_store()

    # Should not raise.
    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
    )

    # We observed the decrypt failure on the first read and bailed —
    # no writes, no second read, no attempt to restore backup.
    store.put_user_token.assert_not_awaited()
    store.delete_user_token.assert_not_awaited()
    assert store.get_user_token.await_count == 1


@pytest.mark.asyncio
async def test_refresh_token_releases_lock_on_decrypt_error() -> None:
    """In cloud mode, the distributed lock is acquired before the
    token read. If decryption fails, the lock must still be released —
    otherwise a single key-mismatch user would hold the per-user lock
    for its full TTL and block every other backend from retrying that
    user for 60s."""
    store = _make_undecryptable_store()
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.release = AsyncMock()

    await refresh_token_for_user(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(),
        user_id=USER_ID,
        connection_store=store,
        server_url=SERVER_URL,
        distributed_lock=lock,
    )

    lock.acquire.assert_awaited_once()
    lock.release.assert_awaited_once()
