"""Teammate actions, shared by the dashboard's Team page and the
Admin MCP's user tools. See :mod:`admin_actions` for why both doors
call these instead of carrying their own copies.

Inviting someone puts their address in the org's users with a role. That
is only an invitation: they become a member when THEY accept it
(``accept_invitation``), which saves their membership row. Until then
they have no access to the org, and the org's admins have no power over
them: removing a pending invitation only deletes it.

Every action that changes something runs to its end once started
(``runs_to_completion``): a removal cut half-way by a cancelled request
used to leave the removed admin an admin in the running policy, still
signed in, with no audit row.

Addresses compare ignoring letter case. An action finds its target once,
as the org stores it (``find_address``): an admin typing
``Bob@Acme.com`` acts on ``bob@acme.com``. An org's users saved before
letter case was ignored may hold two spellings of one person: a removal
or a role change acts on every one (the config stores do it), so no
spelling is left behind as an invitation the removed person could accept
again, or with the old role. A member's own data (sign-ins, sessions,
tokens) is kept under the address they accepted with, which their
membership row holds, so a removal tears it down under that one."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from mcpolis.adapters.observability.analytics_client import (
    email_hash,
    get_analytics,
)
from mcpolis.domain.model.email_address import (
    email_key,
    find_address,
    same_email,
)
from mcpolis.domain.model.settings import SettingsConfig, UserDefinition
from mcpolis.domain.ports.config_repository import UserAlreadyExistsError
from mcpolis.domain.ports.organization_repository import (
    Membership,
    OrganizationRepository,
)
from mcpolis.domain.services.admin_actions import (
    AdminActionDeps,
    AlreadyExists,
    Conflict,
    InvalidRequest,
    NotFound,
    publish_policy_changed,
)
from mcpolis.domain.services.audit_actions import (
    GATEWAY_SIGN_IN_REVOKED,
    MEMBER_INVITED,
    MEMBER_REMOVED,
    MEMBER_ROLE_CHANGED,
    record_action,
)
from mcpolis.domain.services.cancel_shield import runs_to_completion
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.plan_gates import (
    assert_seat_capacity,
    resolve_plan,
)
from mcpolis.domain.services.settings_resolver import (
    LAST_ADMIN_DEMOTE_ERROR,
    LAST_ADMIN_REMOVE_ERROR,
    LastAdminError,
    resolve_settings,
    would_remove_last_admin,
)

UserStatus = Literal["active", "pending"]


class GatewaySignInRevoked(BaseModel):
    """What revoking someone's gateway sign-in ended."""
    tokens_revoked: int
    sessions_closed: int


class UserView(BaseModel):
    """One teammate as both doors show it."""

    email: str
    role: str
    is_admin: bool
    # "active" once they accepted the invitation, "pending" until then.
    status: UserStatus


async def active_member_emails(
    org_repo: OrganizationRepository | None,
    org_id: str,
    config: SettingsConfig,
) -> set[str]:
    """The org's members: the addresses in ``config.users`` that accepted
    their invitation, which saved their membership row (the org's
    creator gets one at creation).

    An address in ``config.users`` with no row is only invited. The Team
    page calls it "pending"; it is not a member: it has no access to the
    org, the org's admins can't act on it (sign it out, close its
    sessions, delete its sign-ins), and the last-admin guard doesn't
    count it (an invitation sent to a typo can never administer
    anything). Addresses compare ignoring letter case.

    Without an org repository (tests that build the services by hand)
    membership is not tracked and everyone in ``config.users`` counts.
    """
    if org_repo is None:
        return set(config.users.keys())
    joined = {email_key(m.email) for m in await org_repo.list_memberships(org_id)}
    return {email for email in config.users if email_key(email) in joined}


def _user_view(
    config: SettingsConfig, email: str, active_emails: set[str],
) -> UserView:
    return UserView(
        email=email,
        role=config.users[email].role,
        is_admin=resolve_settings(config, email).is_admin,
        status="active" if email in active_emails else "pending",
    )


class UserAdminService:
    """List, add, remove and re-role an org's teammates, and let an
    invited person accept or decline their invitation."""

    def __init__(self, deps: AdminActionDeps) -> None:
        self._deps = deps

    async def _active_emails(
        self, org_id: str, config: SettingsConfig,
    ) -> set[str]:
        return await active_member_emails(self._deps.org_repo, org_id, config)

    async def _membership_row(
        self, org_id: str, email: str,
    ) -> Membership | None:
        """``email``'s membership row in the org: the invitation it
        accepted. None while it is only invited, and without an org
        repository."""
        if self._deps.org_repo is None:
            return None
        for row in await self._deps.org_repo.list_memberships(org_id):
            if same_email(row.email, email):
                return row
        return None

    async def _member_of_another_org(self, org_id: str, email: str) -> bool:
        if self._deps.org_repo is None:
            return False
        rows = await self._deps.org_repo.get_memberships_for_email(email)
        return any(row.org_id != org_id for row in rows)

    async def list_users(self, org_id: str) -> list[UserView]:
        runtime = await self._deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        active_emails = await self._active_emails(org_id, config)
        return [_user_view(config, email, active_emails) for email in config.users]

    @runs_to_completion
    async def add_user(
        self,
        org_id: str,
        email: str,
        role: str | None,
        *,
        actor: str,
        source: str,
    ) -> UserView:
        """Invite ``email``. They stay "pending" until they accept the
        invitation (``accept_invitation``), which makes them a member.
        ``source`` names the door for the plan-limit analytics event."""
        runtime = await self._deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        existing = find_address(config.users, email)
        if existing is not None:
            raise AlreadyExists(f"User '{existing}' already exists")
        # The user-config map is the seat ledger: pending and active
        # rows alike count.
        plan = await resolve_plan(self._deps.org_repo, org_id)
        assert_seat_capacity(
            plan, len(config.users),
            source=source, org_id=org_id, actor_email=actor,
        )
        role = role or runtime.policy_engine.get_default_role()
        if role is None:
            raise InvalidRequest("No default role configured")
        if role not in config.roles:
            raise InvalidRequest(f"Role '{role}' not found")
        try:
            new_config = await self._deps.policy_store.set_user(
                org_id, email, UserDefinition(role=role),
            )
        except UserAlreadyExistsError as e:
            # Added by a parallel request (or another backend) after the
            # check above: the store refuses to overwrite its role.
            raise AlreadyExists(str(e)) from None
        except ValueError as e:
            # The role went away (renamed or deleted) after the check
            # above: the store re-checks it under its own lock.
            raise InvalidRequest(str(e)) from None
        runtime.policy_engine.reload(new_config)
        view = _user_view(
            new_config, email, await self._active_emails(org_id, new_config),
        )
        await record_action(
            self._deps.audit_repo, org_id,
            action=MEMBER_INVITED,
            actor=actor,
            target_user_id=email,
            detail=role,
        )
        get_analytics().track_async(
            actor,
            "user_added",
            {
                "target_email_hash": email_hash(email),
                "assigned_role": role,
                "is_admin": view.is_admin,
            },
        )
        return view

    @runs_to_completion
    async def accept_invitation(self, org_id: str, email: str) -> UserView:
        """``email`` accepts its invitation to the org (the Join button):
        it becomes a member, with the role it was invited with.

        This is the only way an invitation becomes a membership: signing
        in never accepts one. Accepting twice is harmless. Raises
        ``NotFound`` when there is no invitation (never invited, or
        removed meanwhile). An invitation typed with other capitals is
        accepted; the row keeps ``email`` as the person signs in with
        it, which their sign-ins are kept under.

        Runs under the org's roles lock, like a role change: the row
        keeps its own copy of the role name, and a rename landing
        between reading the role and saving the row would leave the row
        on a role that no longer exists. A Decline holds it too, so the
        two can't interleave.
        """
        org_repo = self._deps.org_repo
        if org_repo is None:
            raise InvalidRequest("Invitations are not tracked here")
        runtime = await self._deps.runtime_manager.get(org_id)
        no_invitation = NotFound("No invitation to accept")
        async with runtime.roles_lock:
            config = runtime.policy_engine.config
            key = find_address(config.users, email)
            if key is None:
                raise no_invitation
            invited = config.users[key]
            if await self._membership_row(org_id, email) is None:
                await org_repo.add_membership(org_id, email, invited.role)
                if find_address(runtime.policy_engine.config.users, email) is None:
                    # Removed while the row was being saved: that removal
                    # saw no membership to end, so don't leave one.
                    await org_repo.remove_membership(org_id, email)
                    raise no_invitation
                self._deps.runtime_manager.note_member_joined(org_id, email)
                publish_policy_changed(self._deps.event_bus, org_id, user=email)
                get_analytics().track_async(
                    email, "invitation_accepted",
                    {"assigned_role": invited.role},
                )
            config = runtime.policy_engine.config
            key = find_address(config.users, email)
            if key is None:
                raise no_invitation
        return _user_view(config, key, await self._active_emails(org_id, config))

    @runs_to_completion
    async def decline_invitation(self, org_id: str, email: str) -> None:
        """``email`` declines its invitation to the org: the invitation is
        deleted, as if an admin had removed it. Raises ``NotFound`` when
        there is no invitation, ``Conflict`` for a member (a member leaves
        by asking an admin to remove them).

        Runs under the org's roles lock, which a Join holds too: a Join
        clicked in another tab lands either before (the person is then a
        member, and can't decline) or after (nothing left to accept).
        Never a membership row with no invitation behind it, which would
        make a later re-invitation active without its own Join.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        no_invitation = NotFound("No invitation to decline")
        async with runtime.roles_lock:
            config = runtime.policy_engine.config
            key = find_address(config.users, email)
            if key is None:
                raise no_invitation
            if self._deps.org_repo is None or await self._membership_row(
                org_id, email,
            ) is not None:
                raise Conflict("You are already a member of this organization")
            try:
                new_config = await self._deps.policy_store.remove_user(
                    org_id, key,
                    eligible=await self._active_emails(org_id, config),
                )
            except LastAdminError as e:
                raise Conflict(str(e)) from None
            except ValueError:
                raise no_invitation from None
            runtime.policy_engine.reload(new_config)
            # Checked again now the invitation is gone: a row saved by a
            # door that doesn't take this lock would be a membership with
            # no invitation behind it. End it, as a removal would.
            row = await self._membership_row(org_id, email)
            if row is not None:
                await self._end_membership(runtime, org_id, row.email)
        publish_policy_changed(self._deps.event_bus, org_id, user=email)
        get_analytics().track_async(email, "invitation_declined", {})

    @runs_to_completion
    async def remove_user(self, org_id: str, email: str, *, actor: str) -> None:
        """Remove ``email`` from the org.

        A member loses everything they hold in it: their gateway
        sessions on this org, their upstream sign-ins (including one
        still in progress) and upstream sessions, and the membership
        row. Their gateway sign-in belongs to the person, not the org:
        it is revoked only when this was the last org they belonged to,
        so their other orgs keep working.

        A pending invitation is only deleted. The invited person is not
        a member: nothing else of theirs is touched. Otherwise an admin
        could invite any address just to sign that person out.

        Every teardown step is idempotent, so a removal that stopped half
        way (a store failed after ``config.users`` lost the user) is
        finished by removing the same user again: their leftover
        membership row still names them, and the running policy is
        reloaded from the saved config, which no longer has them.

        ``email`` is found once, letter case ignored, as the org stores
        it; ``NotFound`` when neither the users nor a leftover row has
        it. Another spelling of the sole admin is refused like the
        admin's own. Every spelling of the address in the org's users is
        removed, not only the one found.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        target = find_address(config.users, email) or email
        removed = config.users.get(target)
        eligible = await self._active_emails(org_id, config)
        if would_remove_last_admin(config, target, eligible=eligible):
            raise Conflict(LAST_ADMIN_REMOVE_ERROR)
        try:
            new_config = await self._deps.policy_store.remove_user(
                org_id, target, eligible=eligible,
            )
        except LastAdminError as e:
            # The store re-checks under its write lock; a racing request
            # can land here even though the pre-check above passed.
            raise Conflict(str(e)) from None
        except ValueError as e:
            if await self._membership_row(org_id, target) is None:
                raise NotFound(str(e)) from None
            # A removal that stopped half way: the saved config already
            # lost the user, the running policy may still have them.
            new_config = await self._deps.policy_store.load(org_id)
        runtime.policy_engine.reload(new_config)
        # Decided after the config write, so an invitation accepted
        # while the removal ran is ended too. The member's own data is
        # kept under the address their row holds.
        member = await self._member_address(
            org_id, target, in_users=removed is not None,
        )
        publish_policy_changed(
            self._deps.event_bus, org_id, user=member or target,
        )
        if member is not None:
            await self._end_membership(runtime, org_id, member)
        await record_action(
            self._deps.audit_repo, org_id,
            action=MEMBER_REMOVED,
            actor=actor,
            target_user_id=target,
        )
        get_analytics().track_async(
            actor,
            "user_removed",
            {
                "target_email_hash": email_hash(target),
                "removed_role": removed.role if removed else "unknown",
                "was_pending": member is None,
            },
        )

    async def _member_address(
        self, org_id: str, user_key: str, *, in_users: bool,
    ) -> str | None:
        """The address the member at ``user_key`` accepted with (their
        row's), which their sign-ins and sessions are kept under. None for
        a pending invitation. Without an org repository membership is not
        tracked: everyone in the users is a member, under its key."""
        if self._deps.org_repo is None:
            return user_key if in_users else None
        row = await self._membership_row(org_id, user_key)
        return row.email if row is not None else None

    async def _end_membership(
        self, runtime: OrgRuntime, org_id: str, email: str,
    ) -> None:
        """Tear down what a removed member holds in the org. ``email`` is
        the address they accepted with (``_member_address``)."""
        # A sign-in to an upstream still waiting for its callback must
        # not land after the purge below.
        if self._deps.auth_coordinator is not None:
            self._deps.auth_coordinator.abort_for_user(org_id, email)
        # Gateway: close their open sessions on this org (the client
        # gets 404 on its next request). Revoke the gateway sign-in
        # itself only when no other org of theirs still uses it.
        if self._deps.terminate_gateway_sessions is not None:
            await self._deps.terminate_gateway_sessions(org_id, email)
        if (
            self._deps.revoke_gateway_user is not None
            and not await self._member_of_another_org(org_id, email)
        ):
            self._deps.revoke_gateway_user(email)
        # Upstreams: delete ALL per-user OAuth state (tokens + DCR
        # client_info + oauth_metadata + counters; a token-only delete
        # would let a re-invite reuse a dead client_info and hit
        # ``invalid_client``) BEFORE closing the live sessions, so a call
        # still resolving them cannot reconnect from a saved sign-in once
        # its session is gone.
        if self._deps.connection_store is not None:
            await self._deps.connection_store.delete_all_for_user(org_id, email)
        await runtime.client_manager.disconnect_all_user_sessions(email)
        # Last, the membership row: it lists the org among the user's
        # orgs, and it is what lets a retry finish a half-done removal.
        row = await self._membership_row(org_id, email)
        if row is not None and self._deps.org_repo is not None:
            await self._deps.org_repo.remove_membership(org_id, row.email)
        self._deps.runtime_manager.note_member_left(org_id, email)

    @runs_to_completion
    async def revoke_gateway_sign_in(
        self, org_id: str, email: str, *, actor: str,
    ) -> GatewaySignInRevoked:
        """Sign ``email`` out of the gateway: revoke their gateway tokens
        and close their open gateway sessions in this org.

        A gateway sign-in belongs to the person, not the org: revoking it
        signs them out of every org they're in. So an admin may only
        revoke a member of their own org: someone who accepted its
        invitation. A pending invitation does not count, since an admin
        can invite any address without asking. Anyone else gets the same
        ``NotFound`` as "no tokens", which reveals nothing about people
        outside the org. (Super-admins have their own cross-org revoke.)

        Sessions are closed even when no tokens were left (they were
        revoked earlier, from another org): a stream may still be open.
        The revoked bearer is refused on the next request anyway, but an
        open event stream would stay open until the client hung up.
        Sessions on another org's mount are left to that refusal.

        A revoke that removed tokens leaves an audit row naming ``actor``.

        ``email`` is found once among the members, letter case ignored;
        the tokens and sessions are those of the address the member
        accepted with.
        """
        if self._deps.revoke_gateway_user is None:
            raise InvalidRequest("OAuth is not enabled")
        not_found = NotFound(f"No tokens found for {email}")
        runtime = await self._deps.runtime_manager.get(org_id)
        members = await self._active_emails(org_id, runtime.policy_engine.config)
        target = find_address(members, email)
        if target is None:
            raise not_found
        member = await self._member_address(org_id, target, in_users=True)
        if member is None:  # its row went meanwhile: no longer a member
            raise not_found
        revoked = self._deps.revoke_gateway_user(member)
        closed = 0
        if self._deps.terminate_gateway_sessions is not None:
            closed = await self._deps.terminate_gateway_sessions(org_id, member)
        if revoked == 0:
            raise not_found
        await record_action(
            self._deps.audit_repo, org_id,
            action=GATEWAY_SIGN_IN_REVOKED,
            actor=actor,
            target_user_id=target,
        )
        return GatewaySignInRevoked(tokens_revoked=revoked, sessions_closed=closed)

    @runs_to_completion
    async def set_user_role(
        self, org_id: str, email: str, role: str, *, actor: str,
    ) -> UserView:
        """Change a teammate's role (a member's or a pending invitation's).

        Runs under the org's roles lock, so a role rename or delete can't
        land between saving the role and updating the membership row,
        which keeps its own copy of the role name. The row is updated
        only if it exists: a pending invitation gets one when accepted,
        and a removal that already deleted it must not see it come back.

        ``email`` is found once, letter case ignored, as the org stores
        it; ``NotFound`` when the org has no such user. Every spelling of
        the address in the org's users gets the new role.
        """
        runtime = await self._deps.runtime_manager.get(org_id)
        async with runtime.roles_lock:
            config = runtime.policy_engine.config
            target = find_address(config.users, email)
            if target is None:
                raise NotFound(f"User '{email}' not found")
            previous = config.users[target]
            eligible = await self._active_emails(org_id, config)
            # Only pre-check once the target role is known to exist —
            # otherwise a bogus role name on the sole admin reports "only
            # admin" when the real problem is the role name. The store
            # still enforces the invariant either way.
            if role in config.roles and would_remove_last_admin(
                config, target, new_role=role, eligible=eligible,
            ):
                raise Conflict(LAST_ADMIN_DEMOTE_ERROR)
            try:
                new_config = await self._deps.policy_store.set_user_role(
                    org_id, target, role, eligible=eligible,
                )
            except LastAdminError as e:
                raise Conflict(str(e)) from None
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            runtime.policy_engine.reload(new_config)
            member = await self._member_address(org_id, target, in_users=True)
            publish_policy_changed(
                self._deps.event_bus, org_id, user=member or target,
            )
            if self._deps.org_repo is not None and member is not None:
                await self._deps.org_repo.update_membership_role(
                    org_id, member, role,
                )
        view = _user_view(
            new_config, target, await self._active_emails(org_id, new_config),
        )
        await record_action(
            self._deps.audit_repo, org_id,
            action=MEMBER_ROLE_CHANGED,
            actor=actor,
            target_user_id=target,
            detail=f"{previous.role} → {role}",
        )
        get_analytics().track_async(
            actor,
            "user_role_changed",
            {
                "target_email_hash": email_hash(target),
                "from_role": previous.role,
                "to_role": role,
            },
        )
        return view
