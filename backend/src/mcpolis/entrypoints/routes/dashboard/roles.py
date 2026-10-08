"""Roles router (15 routes): the Roles and Access pages.

Each route runs the role action the Admin MCP shares
(``RoleAdminService``): create, rename and delete roles, and every edit
of what a role may use (mcp-access, tool-access, category defaults,
argument constraints). A refusal becomes an HTTP error through the
app-level ``AdminActionRefused`` handler. Every edit returns the role's
new full state.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends

from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
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
    """Pack a role's settings into the wire shape the Access page reads."""
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
    roles = deps.role_admin

    @router.get("/roles", response_model=list[RoleSummary])
    async def list_roles() -> list[RoleSummary]:
        summaries = await roles.list_roles(current_org_id.get())
        return [
            RoleSummary(
                name=summary.name,
                is_admin=summary.is_admin,
                is_default=summary.is_default,
                user_count=summary.user_count,
                service_token_count=summary.service_token_count,
            )
            for summary in summaries
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
        new_config = await roles.set_mcp_access(
            current_org_id.get(), role_name, body.mcp_access,
        )
        return _role_access_info(role_name, new_config)

    @router.put("/roles/{role_name}/mcps/{mcp_id}")
    async def set_role_mcp_access_entry(
        role_name: str,
        mcp_id: str,
        body: SetMcpAccessRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        new_config = await roles.set_mcp_access_entry(
            current_org_id.get(), role_name, mcp_id, body.enabled,
            actor=admin_email,
        )
        return _role_access_info(role_name, new_config)

    @router.put("/roles/{role_name}/auto-enable-new")
    async def set_role_auto_enable_new(
        role_name: str,
        body: SetAutoEnableNewRequest,
    ) -> RoleAccessInfo:
        new_config = await roles.set_auto_enable_new(
            current_org_id.get(), role_name, body.auto_enable_new,
        )
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
        new_config = await roles.set_tool_access_entry(
            current_org_id.get(), role_name, upstream_id, tool_name,
            body.enabled, actor=admin_email,
        )
        return _role_access_info(role_name, new_config)

    @router.delete(
        "/roles/{role_name}/upstreams/{upstream_id}/tools/{tool_name}",
    )
    async def remove_role_tool_access_entry(
        role_name: str, upstream_id: str, tool_name: str,
    ) -> RoleAccessInfo:
        new_config = await roles.remove_tool_access_entry(
            current_org_id.get(), role_name, upstream_id, tool_name,
        )
        return _role_access_info(role_name, new_config)

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/tool-fallback-enabled",
    )
    async def set_role_tool_fallback_enabled(
        role_name: str, upstream_id: str,
        body: SetToolFallbackEnabledRequest,
    ) -> RoleAccessInfo:
        new_config = await roles.set_tool_fallback_enabled(
            current_org_id.get(), role_name, upstream_id,
            body.fallback_enabled,
        )
        return _role_access_info(role_name, new_config)

    @router.put(
        "/roles/{role_name}/upstreams/{upstream_id}/category-defaults/{annotation}",
    )
    async def set_role_tool_category_default(
        role_name: str, upstream_id: str, annotation: str,
        body: SetEnabledRequest,
    ) -> RoleAccessInfo:
        new_config = await roles.set_category_default(
            current_org_id.get(), role_name, upstream_id, annotation,
            body.enabled,
        )
        return _role_access_info(role_name, new_config)

    @router.delete(
        "/roles/{role_name}/upstreams/{upstream_id}/category-defaults/{annotation}",
    )
    async def remove_role_tool_category_default(
        role_name: str, upstream_id: str, annotation: str,
    ) -> RoleAccessInfo:
        new_config = await roles.remove_category_default(
            current_org_id.get(), role_name, upstream_id, annotation,
        )
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
        new_config = await roles.set_argument_constraint(
            current_org_id.get(), role_name, upstream_id, tool_name, arg_name,
            pattern=body.pattern,
            mode=body.mode,
            actor=admin_email,
            source="dashboard.set_role_argument_constraint",
        )
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
        new_config = await roles.remove_argument_constraint(
            current_org_id.get(), role_name, upstream_id, tool_name, arg_name,
        )
        return _role_access_info(role_name, new_config)

    # --- Role CRUD ---

    @router.post("/roles", response_model=RoleAccessInfo, status_code=201)
    async def create_role(
        body: CreateRoleRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        new_config = await roles.create_role(
            current_org_id.get(), body.name,
            copy_from=body.copy_from,
            actor=admin_email,
            source="dashboard.create_role",
        )
        return _role_access_info(body.name, new_config)

    @router.delete("/roles/{role_name}")
    async def delete_role(
        role_name: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        await roles.delete_role(
            current_org_id.get(), role_name, actor=admin_email,
        )
        return {"status": "removed"}

    @router.put("/roles/{role_name}/rename", response_model=RoleAccessInfo)
    async def rename_role(
        role_name: str,
        body: RenameRoleRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> RoleAccessInfo:
        new_config = await roles.rename_role(
            current_org_id.get(), role_name, body.new_name, actor=admin_email,
        )
        return _role_access_info(body.new_name, new_config)

    return router
