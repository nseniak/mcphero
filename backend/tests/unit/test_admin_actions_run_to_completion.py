"""The admin actions both doors share run to their end once started,
whatever cancels the request meanwhile.

The Admin MCP's call wrapper covers its own door
(``test_admin_mcp_cancelled_calls``). The dashboard's routes only await
these same actions, so the actions protect themselves against both kinds
of cancel a dashboard request can get:

- an anyio cancel scope: what Starlette's ``BaseHTTPMiddleware`` does to
  a request whose client went away;
- a native ``Task.cancel()``: what uvicorn does to the request tasks still
  running when its graceful-shutdown time is up. An anyio shield does
  not stop it.

Each test holds the action half-way in a store, cancels it, then lets the
store go on, and checks the action reached its end.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Literal

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.role_admin_service import RoleAdminService
from mcpolis.domain.services.upstream_admin_service import (
    NewUpstreamRequest,
    UpstreamAdminService,
)
from mcpolis.domain.services.user_admin_service import UserAdminService
from tests.unit.factories import (
    Gate,
    GatedConfigStore,
    GatedConnectionStore,
    cancel_natively_while_gated,
    cancel_while_gated,
)
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    DEPUTY_EMAIL,
    AdminParts,
    ConnectsClientManager,
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_config_two_admins,
)

CancelKind = Literal["anyio", "native"]
CANCEL_KINDS: list[CancelKind] = ["anyio", "native"]


async def cancel_while_held(
    kind: CancelKind,
    gate: Gate,
    call: Callable[[], Coroutine[object, object, object]],
) -> None:
    if kind == "anyio":
        await cancel_while_gated(gate, call)
    else:
        await cancel_natively_while_gated(gate, call)


class StartPausesAfterDisconnect(ConnectsClientManager):
    """A Start held right after it dropped the running session, before
    it starts the MCP again. Its connect then goes live at once."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = Gate()

    async def disconnect_upstream(
        self, upstream_id: str, *, reset_state: bool = True,
    ) -> None:
        await super().disconnect_upstream(upstream_id, reset_state=reset_state)
        await self.gate.hold()


class GatewaySignOuts:
    """Records what a member removal did to the person's gateway access."""

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.sessions_closed: list[str] = []

    def revoke(self, email: str) -> int:
        self.revoked.append(email)
        return 1

    async def terminate(self, org_id: str, email: str) -> int:
        del org_id
        self.sessions_closed.append(email)
        return 1


async def seed_members(tmp_path: Path, rows: dict[str, str]) -> None:
    """Accepted invitations: membership rows, email → role."""
    repo = FileOrganizationRepository(tmp_path / "data")
    for email, role in rows.items():
        await repo.add_membership(DEFAULT_ORG_ID, email, role)


async def membership_roles(tmp_path: Path) -> dict[str, str]:
    repo = FileOrganizationRepository(tmp_path / "data")
    return {m.email: m.role for m in await repo.list_memberships(DEFAULT_ORG_ID)}


async def audited(parts: AdminParts, action: str) -> list[str]:
    """The outcome of each audit row of ``action``."""
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=50)
    return [str(r["outcome"]) for r in rows if r["action"] == action]


async def wait_for_audit_row(parts: AdminParts, action: str) -> list[str]:
    """The outcomes of ``action``'s rows, once there is one (a background
    job writes it)."""
    async with asyncio.timeout(5):
        while not (outcomes := await audited(parts, action)):
            await asyncio.sleep(0.01)
    return outcomes


# --- teammates ---


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_member_removal_still_ends_their_access(
    tmp_path: Path, kind: CancelKind,
) -> None:
    await seed_members(tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"})
    config_store = GatedConfigStore(
        tmp_path / "config.json", gated="remove_user", after=True,
    )
    gateway = GatewaySignOuts()
    parts = await make_admin_parts(
        tmp_path,
        config=make_config_two_admins(),
        config_store=config_store,
        revoke_gateway_user=gateway.revoke,
        terminate_gateway_sessions=gateway.terminate,
    )
    users = UserAdminService(parts.action_deps)

    await cancel_while_held(kind, config_store.gate, lambda: users.remove_user(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, actor=ADMIN_EMAIL,
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert DEPUTY_EMAIL not in runtime.policy_engine.config.users
    assert gateway.revoked == [DEPUTY_EMAIL]
    assert gateway.sessions_closed == [DEPUTY_EMAIL]
    assert DEPUTY_EMAIL not in await membership_roles(tmp_path)
    assert await audited(parts, "member_removed") == ["success"]


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_role_change_still_reaches_the_running_policy(
    tmp_path: Path, kind: CancelKind,
) -> None:
    """Cancelled once the role was saved, the running policy kept the old
    role until the next restart, and the membership row too."""
    await seed_members(tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"})
    config_store = GatedConfigStore(
        tmp_path / "config.json", gated="set_user_role", after=True,
    )
    parts = await make_admin_parts(
        tmp_path, config=make_config_two_admins(), config_store=config_store,
    )
    users = UserAdminService(parts.action_deps)

    await cancel_while_held(kind, config_store.gate, lambda: users.set_user_role(
        DEFAULT_ORG_ID, DEPUTY_EMAIL, "user", actor=ADMIN_EMAIL,
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert runtime.policy_engine.config.users[DEPUTY_EMAIL].role == "user"
    assert (await membership_roles(tmp_path))[DEPUTY_EMAIL] == "user"
    assert await audited(parts, "member_role_changed") == ["success"]


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_invitation_still_reaches_the_running_policy(
    tmp_path: Path, kind: CancelKind,
) -> None:
    config_store = GatedConfigStore(
        tmp_path / "config.json", gated="set_user", after=True,
    )
    parts = await make_admin_parts(
        tmp_path, config=make_config_two_admins(), config_store=config_store,
        plan=PlanName.team,
    )
    users = UserAdminService(parts.action_deps)

    await cancel_while_held(kind, config_store.gate, lambda: users.add_user(
        DEFAULT_ORG_ID, "new@example.com", "user",
        actor=ADMIN_EMAIL, source="test",
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert "new@example.com" in runtime.policy_engine.config.users
    assert await audited(parts, "member_invited") == ["success"]


# --- roles ---


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_role_creation_still_reaches_the_running_policy(
    tmp_path: Path, kind: CancelKind,
) -> None:
    config_store = GatedConfigStore(
        tmp_path / "config.json", gated="create_role", after=True,
    )
    parts = await make_admin_parts(
        tmp_path, config=make_config_two_admins(), config_store=config_store,
        plan=PlanName.team,
    )
    roles = RoleAdminService(parts.action_deps)

    await cancel_while_held(kind, config_store.gate, lambda: roles.create_role(
        DEFAULT_ORG_ID, "reader",
        copy_from=None, actor=ADMIN_EMAIL, source="test",
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert "reader" in runtime.policy_engine.config.roles
    assert await audited(parts, "role_created") == ["success"]


# --- upstreams ---


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_add_upstream_leaves_a_stopped_mcp(
    tmp_path: Path, kind: CancelKind,
) -> None:
    config, _ = _config_with_one_stdio()
    store = GatedConnectionStore(tmp_path, gated="set_disabled")
    parts = await make_admin_parts(
        tmp_path, config=config, plan=PlanName.team, connection_store=store,
    )
    upstreams = UpstreamAdminService(parts.action_deps)

    await cancel_while_held(kind, store.gate, lambda: upstreams.add_upstream(
        DEFAULT_ORG_ID,
        NewUpstreamRequest(id="late", display_name="Late", command="echo"),
        actor=ADMIN_EMAIL, source="test",
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "late")
    assert not await store.is_enabled(DEFAULT_ORG_ID, "late")
    assert runtime.client_manager.is_stopped("late")
    assert await audited(parts, "upstream_added") == ["success"]


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_remove_upstream_purges_its_saved_state(
    tmp_path: Path, kind: CancelKind,
) -> None:
    config, mcp_servers = _config_with_one_stdio()
    store = GatedConnectionStore(tmp_path, gated="delete_all_for_upstream")
    await store.put_user_token(
        DEFAULT_ORG_ID, ADMIN_EMAIL, "s0",
        OAuthToken(
            access_token="old", refresh_token="old-r", expires_at=None,
            scopes=[],
        ),
    )
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, connection_store=store,
    )
    upstreams = UpstreamAdminService(parts.action_deps)

    await cancel_while_held(kind, store.gate, lambda: upstreams.remove_upstream(
        DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "s0") is None
    assert await store.get_user_token(DEFAULT_ORG_ID, ADMIN_EMAIL, "s0") is None
    assert await audited(parts, "upstream_removed") == ["success"]


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_stop_is_saved_and_applied(
    tmp_path: Path, kind: CancelKind,
) -> None:
    config, mcp_servers = _config_with_one_stdio()
    store = GatedConnectionStore(tmp_path, gated="set_disabled", after=True)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, connection_store=store,
    )
    upstreams = UpstreamAdminService(parts.action_deps)

    await cancel_while_held(kind, store.gate, lambda: upstreams.stop_upstream(
        DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
    ))

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert not await store.is_enabled(DEFAULT_ORG_ID, "s0")
    assert runtime.client_manager.is_stopped("s0")
    assert await audited(parts, "disconnect") == ["success"]


@pytest.mark.parametrize("kind", CANCEL_KINDS)
async def test_a_cancelled_start_still_starts_the_mcp(
    tmp_path: Path, kind: CancelKind,
) -> None:
    """Cancelled between dropping the running session and starting it
    again, the Start left the MCP down."""
    config, mcp_servers = _config_with_one_stdio()
    manager = StartPausesAfterDisconnect()
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, client_manager=manager,
    )
    store = parts.action_deps.connection_store
    assert store is not None
    await store.set_disabled(DEFAULT_ORG_ID, "s0")
    upstreams = UpstreamAdminService(parts.action_deps)

    await cancel_while_held(kind, manager.gate, lambda: upstreams.start_upstream(
        DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
    ))

    assert await wait_for_audit_row(parts, "reconnect") == ["success"]
    assert await store.is_enabled(DEFAULT_ORG_ID, "s0")
    assert manager.is_connected("s0")
