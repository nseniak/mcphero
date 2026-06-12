"""Roles router (15 routes).

The largest write-side cluster in the dashboard surface — every
``policy_store`` mutation (mcp-access, tool-access, category
defaults, argument constraints, role CRUD) lives here. All routes
fire ``notify_policy_change(role=name)`` on success so connected
gateway sessions re-list tools / re-resolve permissions.

The ``_role_access_info`` helper used to be a closure in
``create_dashboard_api_router``; now a private module-level function.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.domain.model.settings import ArgumentConstraint, SettingsConfig
from mcpolis.domain.services.plan_gates import (
    assert_argument_constraints_allowed,
    assert_custom_role_capacity,
    resolve_plan,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    notify_policy_change,
)
from mcpolis.entrypoints.routes.dashboard._models import (
    CreateRoleRequest,
    RenameRoleRequest,
    RoleAccessInfo,
    RoleSummary,
    SetArgumentConstraintRequest,
    SetAutoEnableNewRequest,
    SetEnabledRequest,
    SetMcpAccessRequest,
    SetRoleMcpAccessRequest,
    SetToolFallbackEnabledRequest,
)


def _role_access_info(name: str, config: SettingsConfig) -> RoleAccessInfo:
    """Pack a role's settings into the wire shape the Access page reads.

    Was a closure inside ``create_dashboard_api_router`` — used by
    every role mutation so they all return the new full role state.
    """
    role = config.roles[name]
    return RoleAccessInfo(
        name=name,
        is_admin=role.is_admin,
        is_default=role.is_default,
        mcp_access=role.settings.mcp_access,
        tool_access=role.settings.tool_access,
        argument_constraints=role.settings.argument_constraints,
    )


def create_roles_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/roles", response_model=list[RoleSummary])
    async def list_roles() -> list[RoleSummary]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        user_counts: dict[str, int] = {}
        for user in config.users.values():
            user_counts[user.role] = user_counts.get(user.role, 0) + 1
        token_counts: dict[str, int] = {}
        if deps.service_token_service is not None:
            token_counts = await deps.service_token_service.count_by_role(
                org_id,
            )
        return [
            RoleSummary(
                name=name,
                is_admin=role.is_admin,
                is_default=role.is_default,
                user_count=user_counts.get(name, 0),
                service_token_count=token_counts.get(name, 0),
            )
            for name, role in config.roles.items()
        ]

    @router.get("/roles/access", response_model=list[RoleAccessInfo])
    async def list_role_access() -> list[RoleAccessInfo]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        config = runtime.policy_engine.config
        return [_role_access_info(name, config) for name in config.roles]

    @router.put("/roles/{role_name}/mcp-access")
    async def set_role_mcp_access(
        role_name: str,
        body: SetRoleMcpAccessRequest,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_mcp_access(
                org_id, role_name, body.mcp_access,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    @router.put("/roles/{role_name}/mcps/{mcp_id}")
    async def set_role_mcp_access_entry(
        role_name: str,
        mcp_id: str,
        body: SetMcpAccessRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_mcp_access_entry(
                org_id, role_name, mcp_id, body.enabled,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        get_analytics().track_async(
            admin_email,
            "role_mcp_access_changed",
            {
                "role_name": role_name,
                "upstream_id": mcp_id,
                "enabled": body.enabled,
            },
        )
        return _role_access_info(role_name, new_config)

    @router.put("/roles/{role_name}/auto-enable-new")
    async def set_role_auto_enable_new(
        role_name: str,
        body: SetAutoEnableNewRequest,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_auto_enable_new(
                org_id, role_name, body.auto_enable_new,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    # --- Tool access endpoints ---

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/tools/{tool_name}",
    )
    async def set_role_tool_access_entry(
        role_name: str, upstream_id: str, tool_name: str,
        body: SetEnabledRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name, body.enabled,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        get_analytics().track_async(
            admin_email,
            "role_tool_access_changed",
            {
                "role_name": role_name,
                "upstream_id": upstream_id,
                "tool_name": tool_name,
                "decision": "allow" if body.enabled else "deny",
            },
        )
        return _role_access_info(role_name, new_config)

    @router.delete(
        "/roles/{role_name}/upstreams/{upstream_id}/tools/{tool_name}",
    )
    async def remove_role_tool_access_entry(
        role_name: str, upstream_id: str, tool_name: str,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.remove_role_tool_access_entry(
                org_id, role_name, upstream_id, tool_name,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/tool-fallback-enabled",
    )
    async def set_role_tool_fallback_enabled(
        role_name: str, upstream_id: str,
        body: SetToolFallbackEnabledRequest,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_tool_fallback_enabled(
                org_id, role_name, upstream_id, body.fallback_enabled,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/category-defaults/{annotation}",
    )
    async def set_role_tool_category_default(
        role_name: str, upstream_id: str, annotation: str,
        body: SetEnabledRequest,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.set_role_tool_category_default(
                org_id, role_name, upstream_id, annotation, body.enabled,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    @router.delete(
        "/roles/{role_name}/upstreams/{upstream_id}/category-defaults/{annotation}",
    )
    async def remove_role_tool_category_default(
        role_name: str, upstream_id: str, annotation: str,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.remove_role_tool_category_default(
                org_id, role_name, upstream_id, annotation,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    # --- Argument constraints ---

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/tools/{tool_name}"
        "/constraints/{arg_name}",
    )
    async def set_role_argument_constraint(
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
        body: SetArgumentConstraintRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        plan = await resolve_plan(deps.org_repo, org_id)
        assert_argument_constraints_allowed(
            plan,
            source="dashboard.set_role_argument_constraint",
            org_id=org_id,
            actor_email=admin_email,
        )
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex: {e}") from None
        if body.mode not in ("allow", "forbid"):
            raise HTTPException(400, f"Invalid mode: {body.mode}")
        constraint = ArgumentConstraint(pattern=body.pattern, mode=body.mode)
        try:
            new_config = await deps.policy_store.set_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name, constraint,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    @router.delete(
        "/roles/{role_name}/upstreams/{upstream_id}/tools/{tool_name}"
        "/constraints/{arg_name}",
    )
    async def remove_role_argument_constraint(
        role_name: str,
        upstream_id: str,
        tool_name: str,
        arg_name: str,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.remove_role_argument_constraint(
                org_id, role_name, upstream_id, tool_name, arg_name,
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(role_name, new_config)

    # --- Role CRUD ---

    @router.post("/roles", response_model=RoleAccessInfo, status_code=201)
    async def create_role(
        body: CreateRoleRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        # Custom-role gate: built-in roles (admin + the is_default
        # role, today "user") don't count. Robust against the
        # past root→default rename — anyone adding a new built-in
        # in the future just needs to flag it ``is_admin`` or
        # ``is_default``.
        plan = await resolve_plan(deps.org_repo, org_id)
        current_custom = sum(
            1 for r in runtime.policy_engine.config.roles.values()
            if not r.is_admin and not r.is_default
        )
        assert_custom_role_capacity(
            plan, current_custom,
            source="dashboard.create_role",
            org_id=org_id,
            actor_email=admin_email,
        )
        try:
            new_config = await deps.policy_store.create_role(
                org_id, body.name, copy_from=body.copy_from,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        runtime.policy_engine.reload(new_config)
        return _role_access_info(body.name, new_config)

    @router.delete("/roles/{role_name}")
    async def delete_role(role_name: str) -> dict[str, str]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        # Service-token guard: the policy store only knows about
        # config.users; tokens reference roles from their own
        # registry, so check here before touching the store.
        if deps.service_token_service is not None:
            token_counts = await deps.service_token_service.count_by_role(
                org_id,
            )
            in_use = token_counts.get(role_name, 0)
            if in_use:
                raise HTTPException(
                    400,
                    f"Cannot delete role '{role_name}': {in_use} service "
                    f"token(s) assigned",
                )
        try:
            new_config = await deps.policy_store.delete_role(org_id, role_name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return {"status": "removed"}

    @router.put("/roles/{role_name}/rename", response_model=RoleAccessInfo)
    async def rename_role(
        role_name: str, body: RenameRoleRequest,
    ) -> RoleAccessInfo:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        try:
            new_config = await deps.policy_store.rename_role(
                org_id, role_name, body.new_name,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        runtime.policy_engine.reload(new_config)
        notify_policy_change(deps, role=role_name)
        return _role_access_info(body.new_name, new_config)

    return router
