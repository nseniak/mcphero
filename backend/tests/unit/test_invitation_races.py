"""The invited person's Join racing their own Decline (clicked in another
tab) or an admin's removal of the invitation.

Whatever the order, a membership row only ever stands with an invitation
behind it. A row left alone makes the org appear in the person's org
list while every page refuses them, and keeps them a member of the
running policy, so a later re-invitation would be active at once,
without its own Join.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path

from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.admin_actions import AdminActionRefused, NotFound
from tests.unit._user_admin_harness import (
    ADMIN,
    UserAdminParts,
    make_user_admin,
    membership_orgs,
)
from tests.unit.factories import GatedConfigStore, run_while_gated
from tests.unit.test_teammate_action_races import MembershipWritePausesOnce

INVITEE = "invitee@example.com"


async def outcome_of(action: Coroutine[object, object, object]) -> str:
    """How a teammate action ended: "done", "not found" or "refused"."""
    try:
        await action
    except NotFound:
        return "not found"
    except AdminActionRefused:
        return "refused"
    return "done"


async def assert_no_membership_left(parts: UserAdminParts) -> None:
    """The invitation is gone, and so is any membership of it."""
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert INVITEE not in runtime.policy_engine.config.users
    assert DEFAULT_ORG_ID not in await membership_orgs(parts, INVITEE)
    assert not runtime.policy_engine.is_member(INVITEE)


async def test_a_join_racing_a_decline_leaves_no_membership_without_an_invitation(
    tmp_path: Path,
) -> None:
    """Both run under the org's roles lock: the Join waits for the
    Decline, then finds nothing to accept. Before, the Join saved the row
    while the Decline was deleting the invitation."""
    store = GatedConfigStore(tmp_path / "config.json", "remove_user")
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", INVITEE: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin")],
        config_store=store,
    )

    declined, joined, overlapped = await run_while_gated(
        store.gate,
        lambda: outcome_of(parts.service.decline_invitation(DEFAULT_ORG_ID, INVITEE)),
        lambda: outcome_of(parts.service.accept_invitation(DEFAULT_ORG_ID, INVITEE)),
    )

    assert not overlapped, "the Join ran while the Decline was under way"
    assert (declined, joined) == ("done", "not found")
    await assert_no_membership_left(parts)
    # A new invitation waits for its own Join.
    await parts.service.add_user(
        DEFAULT_ORG_ID, INVITEE, "user", actor=ADMIN, source="test",
    )
    views = await parts.service.list_users(DEFAULT_ORG_ID)
    assert {v.email: v.status for v in views}[INVITEE] == "pending"
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert not runtime.policy_engine.is_member(INVITEE)


async def test_a_decline_ends_a_membership_saved_while_it_removed_the_invitation(
    tmp_path: Path,
) -> None:
    """A door that doesn't take the roles lock saves a membership row
    while the Decline is deleting the invitation. The Decline checks
    again once the invitation is gone, and ends that membership as a
    removal would."""
    store = GatedConfigStore(tmp_path / "config.json", "remove_user", after=True)
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", INVITEE: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin")],
        config_store=store,
    )
    decline = asyncio.create_task(
        outcome_of(parts.service.decline_invitation(DEFAULT_ORG_ID, INVITEE)),
    )
    await asyncio.wait_for(store.gate.reached.wait(), timeout=5)
    await parts.org_repo.add_membership(DEFAULT_ORG_ID, INVITEE, "user")
    parts.runtime_manager.note_member_joined(DEFAULT_ORG_ID, INVITEE)
    store.gate.release.set()

    assert await decline == "done"
    await assert_no_membership_left(parts)
    assert f"delete_sign_ins:{DEFAULT_ORG_ID}:{INVITEE}" in parts.steps


async def test_a_join_racing_the_removal_of_its_invitation_leaves_no_membership(
    tmp_path: Path,
) -> None:
    """The admin removes the invitation while the Join is saving its row:
    the removal finds no membership to end, so the Join, seeing its
    invitation gone once the row is saved, takes the row back."""
    org_repo = MembershipWritePausesOnce(tmp_path / "data")
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", INVITEE: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin")],
        org_repo=org_repo,
    )
    org_repo.armed = True

    joined, removed, overlapped = await run_while_gated(
        org_repo.gate,
        lambda: outcome_of(parts.service.accept_invitation(DEFAULT_ORG_ID, INVITEE)),
        lambda: outcome_of(
            parts.service.remove_user(DEFAULT_ORG_ID, INVITEE, actor=ADMIN),
        ),
    )

    assert overlapped, "the removal did not land while the Join was saving"
    assert (joined, removed) == ("not found", "done")
    await assert_no_membership_left(parts)
