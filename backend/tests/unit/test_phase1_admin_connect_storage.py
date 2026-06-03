"""Phase 1+2 — admin connect storage shape.

Original bug: an admin who clicks ``Connect`` on the upstream tab for a
``per_user_oauth`` upstream still sees the upstream as
``sign-in required`` on My Tools, because the token is filed under the
synthetic ``ADMIN_USER_ID`` while My Tools looks up by the admin's
real email.

These tests assert the post-fix invariant:

1. ``per_user_oauth`` admin connect → token stored at
   ``(org, admin_email, upstream_id)`` and *not* at ``ADMIN_USER_ID``.
2. ``admin_oauth`` admin connect → token stored under ``ADMIN_USER_ID``
   (unchanged; the admin pool refactor lands in Phase 2).
3. The ``connect_and_refresh_tools`` plumbing relays ``effective_user``
   through to the storage layer with no special casing.

Tests run end-to-end through ``connect_and_refresh_tools`` against a
real ``FileConnectionStore`` so the storage key is observed by name,
not by mocked-out call assertions.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.ports import ADMIN_USER_ID, DEFAULT_ORG_ID
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    connect_and_refresh_tools,
)

from tests.unit.factories import make_upstream_auth, make_upstream_definition


SERVER_URL = "http://localhost:8080"


def make_signing_key() -> bytes:
    return hashlib.sha256(b"test-secret").digest()


def make_oauth_upstream(
    id: str = "mixpanel",
    auth_mode: AuthMode = AuthMode.per_user_oauth,
):
    from mcpolis.domain.model.upstream import TransportType

    return make_upstream_definition(
        id=id,
        transport=TransportType.streamable_http,
        url="http://localhost:9999/mcp",
        auth=make_upstream_auth(mode=auth_mode),
    )


def make_valid_token() -> InternalOAuthToken:
    return InternalOAuthToken(
        access_token="access-stub",
        refresh_token="refresh-stub",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
    )


def make_client_manager() -> MagicMock:
    cm = MagicMock()
    cm.connect_upstream_for_user = AsyncMock()
    return cm


def make_tool_registry() -> MagicMock:
    registry = MagicMock(spec=ToolRegistry)
    registry.refresh_upstream = AsyncMock()
    return registry


@pytest.mark.asyncio
async def test_per_user_oauth_admin_connect_stores_under_admin_email(
    tmp_path: Path,
) -> None:
    """When the admin connect endpoint passes the admin's email as
    ``effective_user`` (Phase 1), the token must end up under that
    email key — not under the synthetic ``ADMIN_USER_ID`` slot.

    Setup uses pre-stored tokens to drive the fast path so the
    OAuth-flow plumbing doesn't enter the test.
    """
    store = FileConnectionStore(tmp_path)
    upstream = make_oauth_upstream(auth_mode=AuthMode.per_user_oauth)
    cm = make_client_manager()
    coord = PendingAuthCoordinator(make_signing_key())
    registry = make_tool_registry()

    admin_email = "alice@example.com"
    # Seed under admin_email so the fast-path connect picks them up.
    await store.put_user_token(
        DEFAULT_ORG_ID, admin_email, upstream.id, make_valid_token(),
    )

    result = await connect_and_refresh_tools(
        DEFAULT_ORG_ID, upstream, admin_email,
        store, coord, cm, registry, SERVER_URL,
    )

    assert result.connected is True
    assert await store.get_user_token(
        DEFAULT_ORG_ID, admin_email, upstream.id,
    ) is not None
    # Critical: no token gets written under ADMIN_USER_ID for
    # per_user_oauth — that was the original bug shape.
    assert await store.get_user_token(
        DEFAULT_ORG_ID, ADMIN_USER_ID, upstream.id,
    ) is None


@pytest.mark.asyncio
async def test_admin_oauth_admin_connect_stores_under_admin_email(
    tmp_path: Path,
) -> None:
    """Phase 2: admin_oauth now also keys by the connecting admin's
    real email. The previous ADMIN_USER_ID slot is no longer written
    by the connect path (legacy reads still consult it as a
    fall-through for upstreams connected pre-Phase-2)."""
    store = FileConnectionStore(tmp_path)
    upstream = make_oauth_upstream(auth_mode=AuthMode.admin_oauth)
    cm = make_client_manager()
    coord = PendingAuthCoordinator(make_signing_key())
    registry = make_tool_registry()

    admin_email = "alice@example.com"
    await store.put_user_token(
        DEFAULT_ORG_ID, admin_email, upstream.id, make_valid_token(),
    )

    result = await connect_and_refresh_tools(
        DEFAULT_ORG_ID, upstream, admin_email,
        store, coord, cm, registry, SERVER_URL,
    )

    assert result.connected is True
    assert await store.get_user_token(
        DEFAULT_ORG_ID, admin_email, upstream.id,
    ) is not None
    # Phase 2 invariant: admin connect no longer writes the synthetic
    # sentinel slot (legacy data may still exist; the resolver tolerates
    # it).
    assert await store.get_user_token(
        DEFAULT_ORG_ID, ADMIN_USER_ID, upstream.id,
    ) is None


@pytest.mark.asyncio
async def test_per_user_oauth_my_tools_lookup_finds_admin_token(
    tmp_path: Path,
) -> None:
    """Original-bug regression: My Tools' ``connection_store.get_user_token
    (org, email, upstream)`` must succeed for the admin who connected
    via the upstream tab.

    Stripped down to the storage contract — the dashboard route's
    My Tools handler is exercised by ``test_dashboard_api`` via the
    same path, so the focus here is the storage key.
    """
    store = FileConnectionStore(tmp_path)
    admin_email = "alice@example.com"
    upstream_id = "mixpanel"
    token = make_valid_token()

    # Phase 1 admin connect simulated: token written under admin email.
    await store.put_user_token(
        DEFAULT_ORG_ID, admin_email, upstream_id, token,
    )

    # My Tools lookup uses (org, viewer_email, upstream_id).
    looked_up = await store.get_user_token(
        DEFAULT_ORG_ID, admin_email, upstream_id,
    )

    assert looked_up is not None
    assert looked_up.access_token == token.access_token


@pytest.mark.asyncio
async def test_admin_user_id_is_not_in_connected_users_list(
    tmp_path: Path,
) -> None:
    """``get_connected_users`` must filter out the synthetic
    ADMIN_USER_ID; the admin tab's connected-users list shows real
    users only. This pre-existing invariant is reasserted here so a
    later refactor does not silently regress it."""
    store = FileConnectionStore(tmp_path)
    upstream_id = "mixpanel"

    await store.put_user_token(
        DEFAULT_ORG_ID, ADMIN_USER_ID, upstream_id, make_valid_token(),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@example.com", upstream_id, make_valid_token(),
    )

    users = await store.get_connected_users(DEFAULT_ORG_ID, upstream_id)

    assert "alice@example.com" in users
    assert ADMIN_USER_ID not in users
