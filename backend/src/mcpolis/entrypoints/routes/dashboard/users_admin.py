"""Admin user-management router (4 routes).

- ``GET /users`` — list (with active/pending status).
- ``POST /users`` — pre-approve a user (no membership row yet).
- ``DELETE /users/{email}`` — full teardown (config + membership +
  gateway tokens + upstream sessions + per-user OAuth rows).
- ``PUT /users/{email}/role`` — change role + propagate to membership.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from mcpolis.adapters.observability.analytics_client import (
    email_hash,
    get_analytics,
)
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.domain.services.plan_gates import (
    assert_seat_capacity,
    resolve_plan,
)
from mcpolis.domain.services.settings_resolver import resolve_settings
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    notify_policy_change,
)
from mcpolis.entrypoints.routes.dashboard._models import (
    AddUserRequest,
    SetRoleRequest,
    UserInfo,
)


def create_users_admin_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/users", response_model=list[UserInfo])
    async def list_users() -> list[UserInfo]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        # Determine which users have actually signed in by checking
        # for a membership row. Users in config.users without a
        # membership are "pending" (pre-approved but never signed in).
        active_emails: set[str] = set()
        if deps.org_repo is not None:
            memberships = await deps.org_repo.list_memberships(org_id)
            active_emails = {m.email for m in memberships}
        else:
            # Standalone mode: everyone is active (no membership
            # tracking, single-org, direct config.users management).
            active_emails = set(config.users.keys())
        results: list[UserInfo] = []
        for email, user_def in config.users.items():
            resolved = resolve_settings(config, email)
            results.append(
                UserInfo(
                    email=email,
                    role=user_def.role,
                    is_admin=resolved.is_admin,
                    status="active" if email in active_emails else "pending",
                ),
            )
        return results

    @router.post("/users", response_model=UserInfo, status_code=201)
    async def add_user(
        body: AddUserRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UserInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        if body.email in config.users:
            raise HTTPException(409, f"User '{body.email}' already exists")
        # Seat gate: the user-config map is the seat ledger (pending +
        # active rows alike count). The helper fires the
        # ``plan_limit_hit`` analytics event before raising.
        plan = await resolve_plan(deps.org_repo, org_id)
        assert_seat_capacity(
            plan, len(config.users),
            source="dashboard.add_user",
            org_id=org_id,
            actor_email=admin_email,
        )
        role = body.role or runtime.policy_engine.get_default_role()
        if role is None:
            raise HTTPException(400, "No default role configured")
        if role not in config.roles:
            raise HTTPException(400, f"Role '{role}' not found")
        user_def = UserDefinition(role=role)
        new_config = await deps.policy_store.set_user(
            org_id, body.email, user_def,
        )
        runtime.policy_engine.reload(new_config)
        # Note: no membership row yet — the user is "pending" until
        # they actually sign in, at which point the login callback
        # creates the membership. This lets the Team page distinguish
        # active (signed in) from pending (pre-approved) users.
        resolved = resolve_settings(new_config, body.email)
        get_analytics().track_async(
            admin_email,
            "user_added",
            {
                "target_email_hash": email_hash(body.email),
                "assigned_role": role,
                "is_admin": resolved.is_admin,
            },
        )
        return UserInfo(
            email=body.email,
            role=role,
            is_admin=resolved.is_admin,
        )

    @router.delete("/users/{email}")
    async def remove_user(
        email: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        removed_role = runtime.policy_engine.config.users.get(email)
        removed_role_name = removed_role.role if removed_role else "unknown"
        try:
            new_config = await deps.policy_store.remove_user(org_id, email)
            runtime.policy_engine.reload(new_config)
            notify_policy_change(deps, user=email)
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        # Remove membership row so list_user_orgs stays in sync.
        if deps.org_repo is not None:
            await deps.org_repo.remove_membership(org_id, email)
        # Terminate active gateway sessions so the client gets 404 on
        # the next request.
        if deps.terminate_gateway_sessions is not None:
            deps.terminate_gateway_sessions(org_id, email)
        # Revoke gateway tokens.
        if deps.revoke_gateway_user is not None:
            deps.revoke_gateway_user(email)
        # Disconnect upstream sessions and tokens.
        await runtime.client_manager.disconnect_all_user_sessions(email)
        if deps.connection_store is not None:
            await deps.connection_store.delete_all_user_tokens(org_id, email)
        get_analytics().track_async(
            admin_email,
            "user_removed",
            {
                "target_email_hash": email_hash(email),
                "removed_role": removed_role_name,
            },
        )
        return {"status": "removed"}

    @router.put("/users/{email}/role")
    async def set_user_role(
        email: str,
        body: SetRoleRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UserInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        previous = runtime.policy_engine.config.users.get(email)
        previous_role = previous.role if previous else "unknown"
        try:
            new_config = await deps.policy_store.set_user_role(
                org_id, email, body.role,
            )
            runtime.policy_engine.reload(new_config)
            notify_policy_change(deps, user=email)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        # Update membership row to keep roles in sync.
        if deps.org_repo is not None:
            memberships = await deps.org_repo.get_memberships_for_email(email)
            if any(m.org_id == org_id for m in memberships):
                await deps.org_repo.add_membership(org_id, email, body.role)
        resolved = resolve_settings(new_config, email)
        get_analytics().track_async(
            admin_email,
            "user_role_changed",
            {
                "target_email_hash": email_hash(email),
                "from_role": previous_role,
                "to_role": body.role,
            },
        )
        return UserInfo(
            email=email,
            role=body.role,
            is_admin=resolved.is_admin,
        )

    return router
