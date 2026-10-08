"""An Admin MCP tool call the client cancels still runs to its end, and
the client's session survives the cancel.

An AI client cancels a call with ``notifications/cancelled`` (its user
pressed Esc). The MCP SDK answers "Request cancelled" at once and cancels
the handler. An action cut there left the org's stores disagreeing: a
removed admin still an admin in the running policy, a new MCP saved as
started, a removed MCP's sign-ins left for a re-add to inherit, a Stop
saved while the MCP kept serving. And the shield that rename and delete
had made the SDK answer the request a second time, which killed the
whole session.

Each test drives the real SDK client and server over the in-memory
transport (``cancel_mcp_call_while_gated``): the client cancels while the
action is held half-way in a store, then the store goes on.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.service_token_service import ServiceTokenService
from tests.unit.factories import (
    Gate,
    GatedConfigStore,
    GatedConnectionStore,
    GatedTokenRepository,
    RecordingEventBus,
    YieldingAuditRepository,
    cancel_mcp_call_while_gated,
)
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    DEPUTY_EMAIL,
    AdminParts,
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_with_custom_role,  # pyright: ignore[reportPrivateUsage]
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    listed_role_names,
    make_admin_parts,
    make_config_two_admins,
)


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


async def member_emails(tmp_path: Path) -> set[str]:
    repo = FileOrganizationRepository(tmp_path / "data")
    return {m.email for m in await repo.list_memberships(DEFAULT_ORG_ID)}


async def audited(parts: AdminParts, action: str) -> list[str]:
    """The outcome of each audit row of ``action``."""
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, limit=50)
    return [str(r["outcome"]) for r in rows if r["action"] == action]


def actions_and_outcomes(rows: list[AuditEntry]) -> list[tuple[str, str]]:
    return [(row.action, row.outcome or "") for row in rows]


# --- roles: the shield that used to crash the session ---


async def test_a_cancelled_rename_finishes_and_the_session_survives(
    tmp_path: Path,
) -> None:
    token_repo = GatedTokenRepository(tmp_path / "data", gated="rename_role")
    tokens = ServiceTokenService(repo=token_repo)
    await tokens.mint(
        org_id=DEFAULT_ORG_ID, label="ci-bot", role_name="reader",
        created_by=ADMIN_EMAIL,
    )
    parts = await make_admin_parts(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        service_token_service=tokens,
    )

    await cancel_mcp_call_while_gated(
        parts.server, token_repo.gate,
        "rename_role", {"role_name": "reader", "new_name": "auditor"},
        caller=ADMIN_EMAIL,
    )

    assert [(t.label, t.role_name) for t in await tokens.list_for_org(
        DEFAULT_ORG_ID,
    )] == [("ci-bot", "auditor")]
    assert await listed_role_names(parts.server) == {"admin", "auditor", "user"}
    assert await audited(parts, "role_renamed") == ["success"]


async def test_a_cancelled_delete_role_still_reloads_the_policy(
    tmp_path: Path,
) -> None:
    """Cancelled after the saved delete, before the reload, the running
    policy would keep the deleted role, so a token could still be minted
    on it."""
    config_store = GatedConfigStore(
        tmp_path / "config.json", gated="delete_role", after=True,
    )
    parts = await make_admin_parts(
        tmp_path,
        config=_config_with_custom_role("reader"),
        plan=PlanName.team,
        config_store=config_store,
    )

    await cancel_mcp_call_while_gated(
        parts.server, config_store.gate,
        "delete_role", {"role_name": "reader"},
        caller=ADMIN_EMAIL,
    )

    assert await listed_role_names(parts.server) == {"admin", "user"}
    assert await audited(parts, "role_deleted") == ["success"]


# --- teammates ---


async def test_a_cancelled_remove_user_still_ends_the_removed_users_access(
    tmp_path: Path,
) -> None:
    """Cancelled once the saved config lost the deputy, the removal used
    to stop there: the deputy stayed an admin in the running policy, kept
    their gateway sign-in, sessions and membership, and no row said what
    happened."""
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

    await cancel_mcp_call_while_gated(
        parts.server, config_store.gate,
        "remove_user", {"email": DEPUTY_EMAIL},
        caller=ADMIN_EMAIL,
    )

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert not runtime.policy_engine.is_admin(DEPUTY_EMAIL)
    assert DEPUTY_EMAIL not in runtime.policy_engine.config.users
    assert gateway.revoked == [DEPUTY_EMAIL]
    assert gateway.sessions_closed == [DEPUTY_EMAIL]
    assert DEPUTY_EMAIL not in await member_emails(tmp_path)
    assert await audited(parts, "member_removed") == ["success"]


async def test_retrying_a_half_done_removal_reloads_the_running_policy(
    tmp_path: Path,
) -> None:
    """A removal that stopped half way (the backend died, or a store
    failed, once the saved config lost the user) is finished by removing
    the same user again. That retry never reloaded the running policy: it
    answered "removed" while the user stayed a member (here an admin) in
    the running app, and in list_users, until a restart."""
    await seed_members(tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"})
    bus = RecordingEventBus()
    gateway = GatewaySignOuts()
    parts = await make_admin_parts(
        tmp_path,
        config=make_config_two_admins(),
        revoke_gateway_user=gateway.revoke,
        terminate_gateway_sessions=gateway.terminate,
        event_bus=bus,
    )
    # Half done: the saved config lost the deputy, while the running
    # policy and the membership row still have them.
    await parts.action_deps.policy_store.remove_user(DEFAULT_ORG_ID, DEPUTY_EMAIL)

    retry = await _call(parts.server, "remove_user", {"email": DEPUTY_EMAIL})

    assert retry == f"User '{DEPUTY_EMAIL}' removed."
    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert not runtime.policy_engine.is_admin(DEPUTY_EMAIL)
    listed = json.loads(await _call(parts.server, "list_users", {}))
    assert DEPUTY_EMAIL not in {user["email"] for user in listed}
    assert DEPUTY_EMAIL not in await member_emails(tmp_path)
    assert gateway.revoked == [DEPUTY_EMAIL]
    assert {"user": DEPUTY_EMAIL} in [
        event.payload for event in bus.events if event.type == "policy_changed"
    ]


# --- upstreams ---


async def test_a_cancelled_add_upstream_leaves_a_stopped_mcp(
    tmp_path: Path,
) -> None:
    """Cancelled between the save and the saved Stop, the new MCP used to
    stay saved as started (the next boot started it) while a retry said
    "already exists"."""
    config, _ = _config_with_one_stdio()
    store = GatedConnectionStore(tmp_path, gated="set_disabled")
    parts = await make_admin_parts(
        tmp_path, config=config, plan=PlanName.team, connection_store=store,
    )

    await cancel_mcp_call_while_gated(
        parts.server, store.gate,
        "add_upstream", {
            "mcp_id": "late", "display_name": "Late",
            "transport": "stdio", "command": "echo",
        },
        caller=ADMIN_EMAIL,
    )

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert await runtime.config_service.get_upstream(DEFAULT_ORG_ID, "late")
    assert not await store.is_enabled(DEFAULT_ORG_ID, "late")
    assert runtime.client_manager.is_stopped("late")
    assert await audited(parts, "upstream_added") == ["success"]


async def test_a_cancelled_remove_upstream_leaves_no_saved_sign_in_behind(
    tmp_path: Path,
) -> None:
    """Cancelled between deleting the definition and purging the MCP's
    saved state, the MCP was gone from every list (nobody retries) while
    its sign-ins and DCR client stayed for a re-add on the same id to
    inherit (``invalid_client``)."""
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

    await cancel_mcp_call_while_gated(
        parts.server, store.gate,
        "remove_upstream", {"mcp_id": "s0"},
        caller=ADMIN_EMAIL,
    )

    listed = json.loads(await _call(parts.server, "list_upstreams", {}))
    assert listed == []
    assert await store.get_user_token(DEFAULT_ORG_ID, ADMIN_EMAIL, "s0") is None
    assert await audited(parts, "upstream_removed") == ["success"]


async def test_a_cancelled_stop_leaves_the_saved_state_and_the_app_agreeing(
    tmp_path: Path,
) -> None:
    """Cancelled once the Stop was saved, the MCP used to keep serving
    while storage said stopped (the next restart stopped it), with no
    audit row."""
    config, mcp_servers = _config_with_one_stdio()
    store = GatedConnectionStore(tmp_path, gated="set_disabled", after=True)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, connection_store=store,
    )

    await cancel_mcp_call_while_gated(
        parts.server, store.gate,
        "disconnect_upstream", {"mcp_id": "s0"},
        caller=ADMIN_EMAIL,
    )

    runtime = await parts.action_deps.runtime_manager.get(DEFAULT_ORG_ID)
    assert not await store.is_enabled(DEFAULT_ORG_ID, "s0")
    assert runtime.client_manager.is_stopped("s0")
    assert await audited(parts, "disconnect") == ["success"]


# --- audit rows ---


async def test_a_cancel_that_lands_while_the_audit_row_is_written_keeps_it(
    tmp_path: Path,
) -> None:
    """The audit store's write waits on the network, like Mongo's insert.
    A cancel landing during it used to cut the write: the Stop happened,
    no row said so."""
    config, mcp_servers = _config_with_one_stdio()
    audit = YieldingAuditRepository(gate=Gate())
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        plan=PlanName.team, audit_repo=audit,
    )
    assert audit.gate is not None

    await cancel_mcp_call_while_gated(
        parts.server, audit.gate,
        "disconnect_upstream", {"mcp_id": "s0"},
        caller=ADMIN_EMAIL,
    )

    assert actions_and_outcomes(audit.rows) == [("disconnect", "success")]
    assert audit.rows[0].user_id == ADMIN_EMAIL
