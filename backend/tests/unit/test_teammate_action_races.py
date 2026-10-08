"""Two admins acting on the same teammate at the same moment.

Through the Admin MCP (the dashboard runs the same shared actions):

- A role change racing a removal must not bring the removed teammate's
  membership row back (they would keep the org in their org list).
- A role change racing a rename of the target role must leave the
  membership row on the new name, not on a role that no longer exists.
- Two parallel removals of the org's two real admins must keep one: a
  pending admin invitation can't administer anything, so the store's
  own last-admin re-check must not count it either.
"""
from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.organization_repository import Membership
from tests.unit.factories import Gate, run_while_gated
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    DEPUTY_EMAIL,
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_with_custom_role,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
    make_config_two_admins,
    read_membership_roles,
    seed_memberships,
)

READER_EMAIL = "reader@example.com"
TYPO_ADMIN = "typo@exmaple.com"


class MembershipWritePausesOnce(FileOrganizationRepository):
    """The first write of a membership role waits at ``gate`` before it
    writes: a role change that has saved the role in the policy and is
    about to bring the membership row in step."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.gate = Gate()
        self.armed = False

    async def _pause(self) -> None:
        if self.armed:
            self.armed = False
            await self.gate.hold()

    async def add_membership(self, org_id: str, email: str, role: str) -> Membership:
        await self._pause()
        return await super().add_membership(org_id, email, role)

    async def update_membership_role(
        self, org_id: str, email: str, role: str,
    ) -> bool:
        await self._pause()
        return await super().update_membership_role(org_id, email, role)


class RemoveUserPausesBeforeWrite(FileConfigStore):
    """The first ``remove_user`` waits at ``gate`` before it touches the
    store (its caller's pre-check has already passed)."""

    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path)
        self.gate = Gate()
        self.armed = True

    async def remove_user(
        self,
        org_id: str,
        email: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        if self.armed:
            self.armed = False
            await self.gate.hold()
        return await super().remove_user(org_id, email, eligible=eligible)


async def make_two_admins_server(
    tmp_path: Path,
) -> tuple[Any, MembershipWritePausesOnce]:
    """Both admins accepted their invitation. The repo is built after the
    rows are on disk: it reads them once, when built."""
    await seed_memberships(
        tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"},
    )
    org_repo = MembershipWritePausesOnce(tmp_path / "data")
    parts = await make_admin_parts(
        tmp_path, config=make_config_two_admins(), org_repo=org_repo,
    )
    return parts.server, org_repo


async def test_a_role_change_racing_a_removal_does_not_bring_the_membership_back(
    tmp_path: Path,
) -> None:
    server, org_repo = await make_two_admins_server(tmp_path)
    org_repo.armed = True

    changed, removed, overlapped = await run_while_gated(
        org_repo.gate,
        lambda: _call(server, "set_user_role", {"email": DEPUTY_EMAIL, "role": "user"}),
        lambda: _call(server, "remove_user", {"email": DEPUTY_EMAIL}),
    )

    assert overlapped
    assert not changed.startswith("Error"), changed
    assert removed == f"User '{DEPUTY_EMAIL}' removed."
    assert DEPUTY_EMAIL not in await read_membership_roles(tmp_path)
    listed = {u["email"] for u in json.loads(await _call(server, "list_users", {}))}
    assert DEPUTY_EMAIL not in listed


async def test_a_role_change_racing_a_rename_keeps_the_membership_on_the_new_name(
    tmp_path: Path,
) -> None:
    config = _config_with_custom_role("reader")
    config["users"][READER_EMAIL] = {"role": "user"}
    await seed_memberships(tmp_path, {ADMIN_EMAIL: "admin", READER_EMAIL: "user"})
    org_repo = MembershipWritePausesOnce(tmp_path / "data")
    parts = await make_admin_parts(
        tmp_path, config=config, org_repo=org_repo, plan=PlanName.team,
    )
    org_repo.armed = True

    changed, renamed, _ = await run_while_gated(
        org_repo.gate,
        lambda: _call(
            parts.server, "set_user_role", {"email": READER_EMAIL, "role": "reader"},
        ),
        lambda: _call(
            parts.server, "rename_role", {"role_name": "reader", "new_name": "auditor"},
        ),
    )

    assert not changed.startswith("Error"), changed
    assert not renamed.startswith("Error"), renamed
    users = {
        u["email"]: u["role"]
        for u in json.loads(await _call(parts.server, "list_users", {}))
    }
    assert users[READER_EMAIL] == "auditor"
    assert (await read_membership_roles(tmp_path))[READER_EMAIL] == "auditor"


async def test_two_parallel_removals_keep_an_admin_who_accepted(
    tmp_path: Path,
) -> None:
    """The service's pre-check counts only admins who accepted their
    invitation; the store's re-check under its lock now counts the same
    ones. Before, the store counted the pending ``typo@`` admin
    invitation, both removals passed, and the org was left with nobody
    able to administer it."""
    config = make_config_two_admins()
    config["users"][TYPO_ADMIN] = {"role": "admin"}  # never accepted
    await seed_memberships(tmp_path, {ADMIN_EMAIL: "admin", DEPUTY_EMAIL: "admin"})
    (tmp_path / "config.json").write_text(json.dumps(config))
    store = RemoveUserPausesBeforeWrite(tmp_path / "config.json")
    parts = await make_admin_parts(tmp_path, config=config, config_store=store)

    first, second, _ = await run_while_gated(
        store.gate,
        lambda: _call(parts.server, "remove_user", {"email": DEPUTY_EMAIL}),
        lambda: _call(parts.server, "remove_user", {"email": ADMIN_EMAIL}),
    )

    stored = await store.load(DEFAULT_ORG_ID)
    accepted_admins = {
        email for email, user in stored.users.items()
        if stored.roles[user.role].is_admin and email != TYPO_ADMIN
    }
    assert accepted_admins, (first, second, sorted(stored.users))
    assert sum(text.startswith("Error") for text in (first, second)) == 1
