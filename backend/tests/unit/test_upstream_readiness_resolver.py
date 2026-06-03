"""Phase A — `_resolve_upstream_readiness` unit tests.

Pins the (auth_mode × admin_count × token state) matrix called out in
``internal/plans/upstream-readiness-uniform-oauth.md`` §Tests. These tests
exercise the resolver function in isolation — no FastAPI app, no HTTP
round-trip — so the design rules are pinned before they reach the
endpoints. Following CLAUDE.md: top-level functions, no fixtures, no
classes; common setup factored into ``make_*`` helpers.

The load-bearing rule under the new design:

    Ready ⇔ at least one **admin** has authenticated.

This is uniform across ``admin_oauth`` and ``per_user_oauth``. The
``test_resolve_readiness_per_user_oauth_only_non_admin_signed_in``
test pins the corollary: a non-admin's signed-in row does NOT make
the upstream Ready — admin presence is the anchor.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.settings import (
    RoleDefinition,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.routes.dashboard._deps import (
    resolve_upstream_readiness as _resolve_upstream_readiness,
)


def make_policy_engine(
    admin_emails: list[str], user_emails: list[str] | None = None,
) -> PolicyEngine:
    users: dict[str, UserDefinition] = {
        email: UserDefinition(role="admin") for email in admin_emails
    }
    for email in user_emails or []:
        users[email] = UserDefinition(role="user")
    config = SettingsConfig(
        roles={
            "admin": RoleDefinition(is_admin=True),
            "user": RoleDefinition(is_default=True),
        },
        users=users,
    )
    return PolicyEngine(config)


def make_runtime(
    policy_engine: PolicyEngine, *, sa_connected: bool = False,
) -> OrgRuntime:
    """Minimal OrgRuntime stub for the resolver — only ``policy_engine``
    and ``client_manager.is_connected`` are read by the resolver.

    ``sa_connected`` controls what ``client_manager.is_connected``
    returns for the service_account branch.
    """
    client_manager = MagicMock()
    client_manager.is_connected = MagicMock(return_value=sa_connected)
    return OrgRuntime(
        org_id=DEFAULT_ORG_ID,
        policy_engine=policy_engine,
        tool_registry=MagicMock(),
        client_manager=client_manager,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=[],
    )


def make_upstream(
    upstream_id: str, mode: AuthMode,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name=upstream_id,
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url="http://localhost:9999/mcp"),
        auth=UpstreamAuthConfig(mode=mode),
    )


def make_token_with_updated_at(updated_at: datetime) -> OAuthToken:
    return OAuthToken(
        access_token="access",
        refresh_token=None,
        expires_at=updated_at + timedelta(hours=1),
        scopes=[],
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------
# service_account
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_readiness_service_account_configured(tmp_path: Path) -> None:
    """``service_account`` is Ready iff the shared session is live;
    ``slot_owner`` is always ``None``."""
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("svc", AuthMode.service_account)

    runtime = make_runtime(make_policy_engine(["alice@co.com"]), sa_connected=True)
    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner is None

    runtime_off = make_runtime(make_policy_engine(["alice@co.com"]), sa_connected=False)
    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime_off,
    )
    assert ready is False
    assert slot_owner is None


# ---------------------------------------------------------------------
# admin_oauth
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_readiness_admin_oauth_mono_admin_pool_empty(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("slack", AuthMode.admin_oauth)
    runtime = make_runtime(make_policy_engine(["alice@co.com"]))

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is False
    assert slot_owner is None


@pytest.mark.asyncio
async def test_resolve_readiness_admin_oauth_mono_admin_signed_in(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("slack", AuthMode.admin_oauth)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack",
        make_token_with_updated_at(datetime.now(UTC)),
    )
    runtime = make_runtime(make_policy_engine(["alice@co.com"]))

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "alice@co.com"


@pytest.mark.asyncio
async def test_resolve_readiness_admin_oauth_multi_admin_takeover(
    tmp_path: Path,
) -> None:
    """A signs in → slot_owner=A. Take-over: A's row deleted, B
    signs in → slot_owner=B. Single-slot semantics keep at most one
    admin row at any time, so the most-recent rule and the only-row
    rule agree.
    """
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("slack", AuthMode.admin_oauth)
    runtime = make_runtime(
        make_policy_engine(["alice@co.com", "bob@co.com"]),
    )

    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "slack",
        make_token_with_updated_at(datetime.now(UTC)),
    )
    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "alice@co.com"

    # Take-over: A's row cleared, B's row added.
    await store.delete_user_token(DEFAULT_ORG_ID, "alice@co.com", "slack")
    await store.put_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "slack",
        make_token_with_updated_at(datetime.now(UTC)),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "bob@co.com"


# ---------------------------------------------------------------------
# per_user_oauth
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_no_signin(tmp_path: Path) -> None:
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(make_policy_engine(["alice@co.com"]))

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is False
    assert slot_owner is None


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_only_non_admin_signed_in(
    tmp_path: Path,
) -> None:
    """Load-bearing test for the new "Ready ⇔ admin authenticated"
    rule. A non-admin's stored token row must NOT make the upstream
    Ready — admin presence is the anchor. A naive "any token row →
    ready" implementation would fail this test.
    """
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(
        make_policy_engine(
            admin_emails=["alice@co.com"],
            user_emails=["carol@co.com"],
        ),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
        make_token_with_updated_at(datetime.now(UTC)),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is False
    assert slot_owner is None


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_admin_signed_in(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(make_policy_engine(["alice@co.com"]))
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(datetime.now(UTC)),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "alice@co.com"


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_admin_plus_non_admin(
    tmp_path: Path,
) -> None:
    """Admin A and non-admin C both signed in. ``ready=True``,
    ``slot_owner=A`` — non-admin row does not contend for the slot.
    """
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(
        make_policy_engine(
            admin_emails=["alice@co.com"],
            user_emails=["carol@co.com"],
        ),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(datetime.now(UTC)),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
        make_token_with_updated_at(datetime.now(UTC) + timedelta(seconds=10)),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "alice@co.com"


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_multi_admin_slot_owner_rule(
    tmp_path: Path,
) -> None:
    """Two admins both have rows for the same per_user_oauth upstream.
    The most-recently updated row wins for ``slot_owner``. Pin the
    rule with two admins whose ``updated_at`` differ by 1 second."""
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(
        make_policy_engine(["alice@co.com", "bob@co.com"]),
    )

    earlier = datetime.now(UTC)
    later = earlier + timedelta(seconds=1)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(earlier),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "notion",
        make_token_with_updated_at(later),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "bob@co.com"

    # Re-write Alice's row with a newer timestamp — slot_owner flips.
    newest = later + timedelta(seconds=1)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(newest),
    )
    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "alice@co.com"


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_admin_disconnect_clears_ready(
    tmp_path: Path,
) -> None:
    """A is the only admin signed in; A disconnects. ``ready=False``
    even if non-admin C still has a row."""
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(
        make_policy_engine(
            admin_emails=["alice@co.com"],
            user_emails=["carol@co.com"],
        ),
    )
    now = datetime.now(UTC)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(now),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
        make_token_with_updated_at(now),
    )

    # Admin disconnects.
    await store.delete_user_token(DEFAULT_ORG_ID, "alice@co.com", "notion")

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is False
    assert slot_owner is None


@pytest.mark.asyncio
async def test_resolve_readiness_per_user_oauth_takeover_clears_a_keeps_c(
    tmp_path: Path,
) -> None:
    """Take-over scenario: admins A + B and non-admin C all signed in.
    B's take-over clears A's row; B is now slot_owner. C's row is
    untouched and C can still invoke (verified at the storage layer)."""
    store = FileConnectionStore(tmp_path)
    upstream = make_upstream("notion", AuthMode.per_user_oauth)
    runtime = make_runtime(
        make_policy_engine(
            admin_emails=["alice@co.com", "bob@co.com"],
            user_emails=["carol@co.com"],
        ),
    )
    now = datetime.now(UTC)
    await store.put_user_token(
        DEFAULT_ORG_ID, "alice@co.com", "notion",
        make_token_with_updated_at(now),
    )
    await store.put_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
        make_token_with_updated_at(now),
    )

    # B takes over: A's row cleared (the take-over flow's
    # responsibility — simulated here at the storage level), B writes
    # their own.
    await store.delete_user_token(DEFAULT_ORG_ID, "alice@co.com", "notion")
    await store.put_user_token(
        DEFAULT_ORG_ID, "bob@co.com", "notion",
        make_token_with_updated_at(now + timedelta(seconds=1)),
    )

    ready, slot_owner = await _resolve_upstream_readiness(
        upstream, DEFAULT_ORG_ID, store, runtime,
    )
    assert ready is True
    assert slot_owner == "bob@co.com"

    # C's row preserved through the take-over.
    carol_token = await store.get_user_token(
        DEFAULT_ORG_ID, "carol@co.com", "notion",
    )
    assert carol_token is not None
