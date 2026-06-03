"""Phase 2 — single-slot admin_oauth invariants.

UX directive: only one admin owns the shared connection at a time.
Admin B cannot click ``Connect`` while admin A is connected — they
must explicitly disconnect A first, then connect themselves. The
backend enforces the invariant via:

1. The connect handler returns ``409`` when another admin already
   owns the slot.
2. The disconnect handler clears the active admin's stored token (and
   tears down their session) regardless of which admin called it,
   so the take-over flow ("B disconnects A, then B connects") works.
3. ``per_user_oauth`` is unaffected — every user's connect always
   stores under their own email; multiple users coexist without
   conflict.

Tests directly exercise the helper ``_admin_oauth_owner`` and the
``connection_store`` round-trip, which is what the route handlers
delegate to. Per CLAUDE.md: top-level tests, no fixtures, factor
common setup into ``make_*`` helpers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as InternalOAuthToken,
)
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import (
    RoleDefinition,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.routes.dashboard._deps import (
    admin_oauth_owner as _admin_oauth_owner,
)

from tests.unit.factories import make_upstream_auth, make_upstream_definition


def make_policy_engine_with_admins(emails: list[str]) -> PolicyEngine:
    config = SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True),
            "user": RoleDefinition(is_default=True),
        },
        users={email: UserDefinition(role="admin") for email in emails},
    )
    return PolicyEngine(config)


def make_token(access_token: str = "x") -> InternalOAuthToken:
    return InternalOAuthToken(
        access_token=access_token,
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )


def make_admin_oauth_upstream(upstream_id: str = "slack"):
    from mcpolis.domain.model.upstream import TransportType

    return make_upstream_definition(
        id=upstream_id,
        transport=TransportType.streamable_http,
        url="http://localhost:9999/mcp",
        auth=make_upstream_auth(mode=AuthMode.admin_oauth),
    )


@pytest.mark.asyncio
async def test_owner_returns_admin_with_stored_token(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com", "bob@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert owner == "alice@co.com"


@pytest.mark.asyncio
async def test_owner_excludes_caller_so_self_reconnect_is_allowed(
    tmp_path: Path,
) -> None:
    """When alice is the slot owner and alice is also the caller, the
    helper returns ``None`` so the connect handler does not 409 her own
    re-OAuth. (B would still be blocked — see next test.)"""
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email="alice@co.com",
        policy_engine=policy,
    )
    assert owner is None


@pytest.mark.asyncio
async def test_owner_blocks_second_admin(tmp_path: Path) -> None:
    """When alice is connected, bob's attempt to connect surfaces
    alice as the owner (so the route returns 409)."""
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com", "bob@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token(),
    )

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email="bob@co.com",
        policy_engine=policy,
    )
    assert owner == "alice@co.com"


@pytest.mark.asyncio
async def test_owner_returns_none_when_no_admin_connected(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com"])

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert owner is None


@pytest.mark.asyncio
async def test_owner_ignores_non_admin_users(tmp_path: Path) -> None:
    """A regular user's stored token must NOT count as ownership of
    the admin_oauth slot. Privacy invariant."""
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "slack", make_token(),
    )

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert owner is None


@pytest.mark.asyncio
async def test_take_over_flow_disconnect_then_connect(
    tmp_path: Path,
) -> None:
    """Simulates the take-over flow at the storage layer:
    1. Alice's token in slot.
    2. Bob disconnects alice → alice's token is gone.
    3. Bob's connect now allowed (no owner) → bob's token in slot.
    """
    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com", "bob@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack", make_token("alice"),
    )

    # Step 2: disconnect simulates the route handler clearing the
    # active owner's slot.
    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert owner == "alice@co.com"
    await store.delete_user_token(
        DEFAULT_ORG_ID, owner or "", "slack",
    )

    # Step 3: bob's connect-time guard sees no owner.
    owner_after = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email="bob@co.com",
        policy_engine=policy,
    )
    assert owner_after is None

    await store.put_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "slack", make_token("bob"),
    )

    final_owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert final_owner == "bob@co.com"


@pytest.mark.asyncio
async def test_per_user_oauth_two_admins_coexist(tmp_path: Path) -> None:
    """``per_user_oauth`` is multi-slot by definition — two admins
    each store their own personal token without any take-over.
    Distinct from admin_oauth's single-slot invariant."""
    store = FileConnectionStore(tmp_path)

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion", make_token("alice"),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "notion", make_token("bob"),
    )

    alice_token = await store.get_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
    )
    bob_token = await store.get_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "notion",
    )

    assert alice_token is not None
    assert bob_token is not None
    assert alice_token.access_token != bob_token.access_token


@pytest.mark.asyncio
async def test_legacy_admin_user_id_token_does_not_block_connect(
    tmp_path: Path,
) -> None:
    """A pre-Phase-2 token under ADMIN_USER_ID is not an admin email,
    so the owner helper ignores it. New admin connects can succeed
    against an upstream that still has legacy data — the legacy slot
    is the tool router's fallback, not a connect-time blocker."""
    from mcpolis.domain.ports import ADMIN_USER_ID

    store = FileConnectionStore(tmp_path)
    policy = make_policy_engine_with_admins(["alice@co.com"])

    await store.put_user_token(
        DEFAULT_ORG_ID, ADMIN_USER_ID, "slack", make_token(),
    )

    owner = await _admin_oauth_owner(
        store, DEFAULT_ORG_ID, "slack", excluding_email=None,
        policy_engine=policy,
    )
    assert owner is None
