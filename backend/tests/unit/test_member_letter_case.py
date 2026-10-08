"""The letter case of an address carries no meaning for the teammate
actions both admin doors share.

An admin may type ``Bob@Example.com`` for the member ``bob@example.com``
(an AI client calling the Admin MCP's ``remove_user`` often does), and
an invitation typed with capitals is accepted by the same address in
lower case. Each action finds its target once, as the org stores it, and
tears a member's own data (gateway sessions and sign-in, upstream
sign-ins) down under the address they accepted with, which their
membership row keeps and their sign-ins are saved under.

Before, a removal under another spelling deleted bob's row but kept his
upstream sign-in and left him on the Team page as "pending"; removing
the real spelling afterwards skipped the teardown, so the sign-in
survived for good; and another spelling of the sole admin removed the
org's only admin.

Only ASCII letter case is ignored (``straße@`` is not ``strasse@``). An
org saved before letter case was ignored may hold one person under two
spellings: a removal or a role change acts on both, and they count as
one admin.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.domain.model.settings import RoleDefinition, SettingsConfig, UserDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.admin_actions import AlreadyExists, Conflict, NotFound
from mcpolis.domain.services.audit_actions import (
    MEMBER_REMOVED,
    MEMBER_ROLE_CHANGED,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.settings_resolver import would_remove_last_admin
from tests.unit._user_admin_harness import (
    ADMIN,
    UserAdminParts,
    make_user_admin,
    membership_orgs,
)

BOB = "bob@example.com"
BOB_OTHER_CASE = "Bob@Example.com"


def make_token() -> OAuthToken:
    return OAuthToken(
        access_token="bob-at", refresh_token="bob-rt",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scopes=[],
    )


def teardown_of(email: str) -> list[str]:
    """What removing a member of ``default`` with no other org ends, in
    order (see ``_user_admin_harness``)."""
    return [
        f"close_gateway_sessions:{DEFAULT_ORG_ID}:{email}",
        f"revoke_gateway_sign_in:{email}",
        f"delete_sign_ins:{DEFAULT_ORG_ID}:{email}",
        f"close_upstream_sessions:{email}",
    ]


async def make_team_with_bob(tmp_path: Path) -> UserAdminParts:
    """``default`` with the admin and bob, both members; bob holds an
    upstream sign-in."""
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", BOB: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin"), (DEFAULT_ORG_ID, BOB, "user")],
    )
    await parts.connection_store.put_user_token(
        DEFAULT_ORG_ID, BOB, "notion", make_token(),
    )
    return parts


async def team_page(parts: UserAdminParts) -> dict[str, str]:
    return {
        view.email: view.status
        for view in await parts.service.list_users(DEFAULT_ORG_ID)
    }


async def audited_teammates(parts: UserAdminParts, action: str) -> list[str | None]:
    """The teammate named by each audit row of ``action``: as the Team
    page lists them, whatever spelling the admin typed."""
    rows = await parts.audit_repo.search(DEFAULT_ORG_ID, action=[action])
    return [row.get("target_user_id") for row in rows]


# --- Acting on another spelling of a member ---


async def test_removing_another_spelling_removes_the_member_entirely(
    tmp_path: Path,
) -> None:
    parts = await make_team_with_bob(tmp_path)

    await parts.service.remove_user(DEFAULT_ORG_ID, BOB_OTHER_CASE, actor=ADMIN)

    assert parts.steps == teardown_of(BOB)
    assert await parts.connection_store.get_user_token(
        DEFAULT_ORG_ID, BOB, "notion",
    ) is None
    assert await membership_orgs(parts, BOB) == set()
    assert await team_page(parts) == {ADMIN: "active"}
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert not runtime.policy_engine.is_member(BOB)
    assert await audited_teammates(parts, MEMBER_REMOVED) == [BOB]
    # Removed once: any spelling now finds nobody.
    with pytest.raises(NotFound):
        await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=ADMIN)


async def test_the_sole_admin_cannot_be_removed_under_another_spelling(
    tmp_path: Path,
) -> None:
    parts = await make_user_admin(
        tmp_path,
        users={BOB: "admin", "dev@example.com": "user"},
        memberships=[
            (DEFAULT_ORG_ID, BOB, "admin"),
            (DEFAULT_ORG_ID, "dev@example.com", "user"),
        ],
    )

    with pytest.raises(Conflict):
        await parts.service.remove_user(DEFAULT_ORG_ID, BOB_OTHER_CASE, actor=BOB)

    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert runtime.policy_engine.get_admin_emails() == [BOB]
    assert await membership_orgs(parts, BOB) == {DEFAULT_ORG_ID}
    # The rule itself, for any caller: another spelling is the same admin.
    config = runtime.policy_engine.config
    assert would_remove_last_admin(config, BOB_OTHER_CASE)
    assert would_remove_last_admin(config, BOB_OTHER_CASE, new_role="user")


async def test_changing_the_role_of_another_spelling_changes_the_members_role(
    tmp_path: Path,
) -> None:
    parts = await make_team_with_bob(tmp_path)

    view = await parts.service.set_user_role(
        DEFAULT_ORG_ID, BOB_OTHER_CASE, "admin", actor=ADMIN,
    )

    assert (view.email, view.role, view.is_admin, view.status) == (
        BOB, "admin", True, "active",
    )
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert sorted(runtime.policy_engine.config.users) == [ADMIN, BOB]
    rows = await parts.org_repo.list_memberships(DEFAULT_ORG_ID)
    assert {row.email: row.role for row in rows}[BOB] == "admin"
    assert await audited_teammates(parts, MEMBER_ROLE_CHANGED) == [BOB]


async def test_changing_the_role_of_nobody_is_not_found(tmp_path: Path) -> None:
    parts = await make_team_with_bob(tmp_path)

    with pytest.raises(NotFound):
        await parts.service.set_user_role(
            DEFAULT_ORG_ID, "ghost@example.com", "admin", actor=ADMIN,
        )


async def test_revoking_another_spelling_signs_the_member_out(
    tmp_path: Path,
) -> None:
    parts = await make_team_with_bob(tmp_path)

    outcome = await parts.service.revoke_gateway_sign_in(
        DEFAULT_ORG_ID, BOB_OTHER_CASE, actor=ADMIN,
    )

    assert outcome.tokens_revoked == 2
    assert parts.revoked == [BOB]
    assert parts.steps == [
        f"revoke_gateway_sign_in:{BOB}",
        f"close_gateway_sessions:{DEFAULT_ORG_ID}:{BOB}",
    ]


async def test_inviting_another_spelling_of_a_teammate_is_refused(
    tmp_path: Path,
) -> None:
    parts = await make_team_with_bob(tmp_path)

    with pytest.raises(AlreadyExists):
        await parts.service.add_user(
            DEFAULT_ORG_ID, BOB_OTHER_CASE, "user", actor=ADMIN, source="test",
        )

    assert await team_page(parts) == {ADMIN: "active", BOB: "active"}


# --- A member who joined under another spelling than the invitation ---


async def make_bob_joined_from_a_capitalized_invitation(
    tmp_path: Path,
) -> UserAdminParts:
    """The admin invited ``Bob@Example.com``; bob accepted it signed in
    as ``bob@example.com`` and holds an upstream sign-in under it."""
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", BOB_OTHER_CASE: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin")],
    )
    await parts.service.accept_invitation(DEFAULT_ORG_ID, BOB)
    await parts.connection_store.put_user_token(
        DEFAULT_ORG_ID, BOB, "notion", make_token(),
    )
    return parts


async def test_accepting_a_capitalized_invitation_makes_a_member(
    tmp_path: Path,
) -> None:
    parts = await make_bob_joined_from_a_capitalized_invitation(tmp_path)

    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert runtime.policy_engine.is_member(BOB)
    assert runtime.policy_engine.get_user_roles(BOB) == ["user"]
    # The Team page keeps the address as the admin typed it; the row
    # keeps it as bob signs in with it.
    assert await team_page(parts) == {ADMIN: "active", BOB_OTHER_CASE: "active"}
    rows = await parts.org_repo.get_memberships_for_email(BOB)
    assert [row.org_id for row in rows] == [DEFAULT_ORG_ID]


async def test_removing_a_member_ends_what_they_hold_under_the_address_they_joined_with(
    tmp_path: Path,
) -> None:
    parts = await make_bob_joined_from_a_capitalized_invitation(tmp_path)

    # As the Team page shows it.
    await parts.service.remove_user(DEFAULT_ORG_ID, BOB_OTHER_CASE, actor=ADMIN)

    assert parts.steps == teardown_of(BOB)
    assert await parts.connection_store.get_user_token(
        DEFAULT_ORG_ID, BOB, "notion",
    ) is None
    assert await membership_orgs(parts, BOB) == set()
    assert await team_page(parts) == {ADMIN: "active"}


async def test_a_role_change_reaches_the_row_of_a_member_who_joined_under_another_spelling(
    tmp_path: Path,
) -> None:
    parts = await make_bob_joined_from_a_capitalized_invitation(tmp_path)

    await parts.service.set_user_role(
        DEFAULT_ORG_ID, BOB_OTHER_CASE, "admin", actor=ADMIN,
    )

    rows = await parts.org_repo.list_memberships(DEFAULT_ORG_ID)
    assert {row.email: row.role for row in rows} == {ADMIN: "admin", BOB: "admin"}
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    # Callers look up the admins' upstream sign-ins with these, which
    # are saved under the address each admin signs in with.
    assert runtime.policy_engine.get_admin_emails() == [ADMIN, BOB]


def test_an_admins_address_is_the_one_they_joined_with() -> None:
    policy = PolicyEngine(
        SettingsConfig(
            roles={"admin": RoleDefinition(is_admin=True)},
            users={"Carol@Example.com": UserDefinition(role="admin")},
        ),
        members=["carol@example.com"],
    )

    assert policy.is_admin("carol@example.com")
    assert policy.address_of("Carol@Example.com") == "carol@example.com"
    assert policy.get_admin_emails() == ["carol@example.com"]


# --- Only ASCII letter case is ignored ---


async def test_another_mailbox_cannot_accept_the_invitation(tmp_path: Path) -> None:
    """``straße@`` is another mailbox than ``strasse@``: Unicode case
    folding called them one person, so ``straße@`` joined as admin with
    the invitation sent to ``strasse@``."""
    invited, other_mailbox = "strasse@example.de", "straße@example.de"
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", invited: "admin"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin")],
    )

    with pytest.raises(NotFound):
        await parts.service.accept_invitation(DEFAULT_ORG_ID, other_mailbox)

    assert await membership_orgs(parts, other_mailbox) == set()
    assert await team_page(parts) == {ADMIN: "active", invited: "pending"}


# --- Two spellings of one person, saved before letter case was ignored ---


async def make_team_with_bob_twice(tmp_path: Path) -> UserAdminParts:
    """The org's users hold bob under two spellings (an admin invited
    ``Bob@Example.com`` while ``bob@example.com`` was there, back when
    invitations compared exactly); bob is a member."""
    return await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", BOB: "user", BOB_OTHER_CASE: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin"), (DEFAULT_ORG_ID, BOB, "user")],
    )


async def test_a_removed_member_cannot_rejoin_through_a_leftover_spelling(
    tmp_path: Path,
) -> None:
    """Removing bob deleted one spelling: the other stayed in the org's
    users as a pending invitation, and bob rejoined with one Join."""
    parts = await make_team_with_bob_twice(tmp_path)

    await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=ADMIN)
    with pytest.raises(NotFound):
        await parts.service.accept_invitation(DEFAULT_ORG_ID, BOB)

    assert await membership_orgs(parts, BOB) == set()
    assert await team_page(parts) == {ADMIN: "active"}
    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert not runtime.policy_engine.is_member(BOB)


async def test_a_role_change_reaches_every_spelling(tmp_path: Path) -> None:
    parts = await make_team_with_bob_twice(tmp_path)

    await parts.service.set_user_role(DEFAULT_ORG_ID, BOB, "admin", actor=ADMIN)

    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    users = runtime.policy_engine.config.users
    assert {email: users[email].role for email in users} == {
        ADMIN: "admin", BOB: "admin", BOB_OTHER_CASE: "admin",
    }


async def test_a_sole_admin_held_under_two_spellings_is_one_admin(
    tmp_path: Path,
) -> None:
    """Counted as two admins, either spelling looked removable, and a
    removal of every spelling would leave the org with no admin."""
    parts = await make_user_admin(
        tmp_path,
        users={BOB: "admin", BOB_OTHER_CASE: "admin", "dev@example.com": "user"},
        memberships=[
            (DEFAULT_ORG_ID, BOB, "admin"),
            (DEFAULT_ORG_ID, "dev@example.com", "user"),
        ],
    )

    with pytest.raises(Conflict):
        await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=BOB)
    with pytest.raises(Conflict):
        await parts.service.set_user_role(DEFAULT_ORG_ID, BOB, "user", actor=BOB)

    runtime = await parts.runtime_manager.get(DEFAULT_ORG_ID)
    assert runtime.policy_engine.is_admin(BOB)
    assert await membership_orgs(parts, BOB) == {DEFAULT_ORG_ID}
