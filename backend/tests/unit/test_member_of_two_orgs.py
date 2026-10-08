"""Removing, or signing out of the gateway, a person who belongs to two
orgs.

A gateway sign-in belongs to the person, not to an org: one bearer
serves every org they are a member of. So:

- Removing them from one org ends only that org's access: their open
  gateway sessions on it close, their upstream sign-ins there are
  deleted, their membership row there goes. Their gateway sign-in stays
  while another org still uses it (it is revoked when the removal takes
  their last org).
- An org admin's "Disconnect" (gateway sign-in revoke) is a sign-out of
  the gateway itself: it ends the person's sign-in everywhere (their
  AI client signs in again) but leaves every membership alone. That is
  why only a member of the admin's own org can be revoked.
"""
from __future__ import annotations

from pathlib import Path

from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.audit_actions import GATEWAY_SIGN_IN_REVOKED
from tests.unit._dev_stub_login import login_as
from tests.unit._user_admin_harness import (
    ADMIN,
    OTHER_ORG,
    UserAdminParts,
    make_user_admin,
    membership_orgs,
)
from tests.unit.test_dashboard_api import make_test_client, seed_gateway_tokens

BOB = "bob@example.com"


async def make_bob_in_two_orgs(tmp_path: Path) -> UserAdminParts:
    return await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", BOB: "user"},
        memberships=[
            (DEFAULT_ORG_ID, ADMIN, "admin"),
            (DEFAULT_ORG_ID, BOB, "user"),
            (OTHER_ORG, BOB, "user"),
        ],
    )


async def test_removing_a_member_of_two_orgs_ends_only_that_orgs_access(
    tmp_path: Path,
) -> None:
    parts = await make_bob_in_two_orgs(tmp_path)

    await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=ADMIN)

    assert parts.steps == [
        f"close_gateway_sessions:{DEFAULT_ORG_ID}:{BOB}",
        f"delete_sign_ins:{DEFAULT_ORG_ID}:{BOB}",
        f"close_upstream_sessions:{BOB}",
    ]
    assert parts.revoked == []
    assert await membership_orgs(parts, BOB) == {OTHER_ORG}


async def test_removal_from_their_last_org_also_signs_them_out_of_the_gateway(
    tmp_path: Path,
) -> None:
    parts = await make_user_admin(
        tmp_path,
        users={ADMIN: "admin", BOB: "user"},
        memberships=[(DEFAULT_ORG_ID, ADMIN, "admin"), (DEFAULT_ORG_ID, BOB, "user")],
    )

    await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=ADMIN)

    assert parts.revoked == [BOB]
    assert await membership_orgs(parts, BOB) == set()


async def test_removal_deletes_upstream_sign_ins_before_closing_their_sessions(
    tmp_path: Path,
) -> None:
    """Like a member's own sign-out: a call still resolving the removed
    member must not reconnect from a saved sign-in once its session is
    gone."""
    parts = await make_bob_in_two_orgs(tmp_path)

    await parts.service.remove_user(DEFAULT_ORG_ID, BOB, actor=ADMIN)

    deleted = parts.steps.index(f"delete_sign_ins:{DEFAULT_ORG_ID}:{BOB}")
    closed = parts.steps.index(f"close_upstream_sessions:{BOB}")
    assert deleted < closed


async def test_an_admins_gateway_revoke_signs_a_two_org_member_out_everywhere(
    tmp_path: Path,
) -> None:
    """The revoke itself is global (one sign-in serves both orgs); only
    this org's open sessions are closed here, and no membership ends."""
    parts = await make_bob_in_two_orgs(tmp_path)

    outcome = await parts.service.revoke_gateway_sign_in(
        DEFAULT_ORG_ID, BOB, actor=ADMIN,
    )

    assert (outcome.tokens_revoked, outcome.sessions_closed) == (2, 1)
    assert parts.steps == [
        f"revoke_gateway_sign_in:{BOB}",
        f"close_gateway_sessions:{DEFAULT_ORG_ID}:{BOB}",
    ]
    assert await membership_orgs(parts, BOB) == {DEFAULT_ORG_ID, OTHER_ORG}


async def test_an_org_admins_gateway_revoke_is_audited(tmp_path: Path) -> None:
    """The operator's sign-out-everywhere leaves a row; so does the org
    admin's own revoke, naming who did it and to whom."""
    client = make_test_client(tmp_path)
    seed_gateway_tokens(client, "dev@example.com")

    resp = client.delete("/api/admin/gateway/users/dev%40example.com")

    assert resp.status_code == 200, resp.text
    rows = client.get(
        "/api/admin/audit", params={"action": GATEWAY_SIGN_IN_REVOKED},
    ).json()["entries"]
    assert [(r["user_id"], r["target_user_id"]) for r in rows] == [
        (ADMIN, "dev@example.com"),
    ]


async def test_a_revoke_that_found_no_tokens_leaves_no_audit_row(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    login_as(client, ADMIN)

    resp = client.delete("/api/admin/gateway/users/dev%40example.com")

    assert resp.status_code == 404, resp.text
    rows = client.get(
        "/api/admin/audit", params={"action": GATEWAY_SIGN_IN_REVOKED},
    ).json()["entries"]
    assert rows == []
