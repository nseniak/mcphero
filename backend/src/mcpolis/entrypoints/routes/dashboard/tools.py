"""Tools router (2 routes).

- ``GET /api/admin/tools`` — every prefixed-tool across every upstream.
- ``GET /api/admin/admin-mcp/tools`` — the admin-MCP tool catalog.

The per-upstream variant ``/upstreams/{id}/tools`` lives in
``upstream_admin.py`` (URL grouping wins over conceptual grouping).
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    ToolAnnotationsInfo,
    ToolInfo,
)


# Categories surfaced to the admin chat sidebar's tool-picker. Same
# bucketing the dashboard reads from
# ``GET /api/admin/admin-mcp/tools``.
_TOOL_CATEGORIES: dict[str, str] = {
    "list_upstreams": "Upstream management",
    "get_upstream": "Upstream management",
    "add_upstream": "Upstream management",
    "remove_upstream": "Upstream management",
    "connect_upstream": "Upstream management",
    "disconnect_upstream": "Upstream management",
    "upstream_status": "Upstream management",
    "check_upstream_auth_status": "Upstream management",
    "refresh_upstream_tools": "Upstream management",
    "list_upstream_tools": "Upstream management",
    "list_default_arguments": "Tool customization",
    "set_default_arguments": "Tool customization",
    "remove_default_arguments": "Tool customization",
    "list_users": "User management",
    "add_user": "User management",
    "remove_user": "User management",
    "set_user_role": "User management",
    "list_roles": "Role management",
    "create_role": "Role management",
    "delete_role": "Role management",
    "rename_role": "Role management",
    "set_role_mcp_access": "Access policies",
    "set_role_auto_enable_new": "Access policies",
    "set_role_tool_access": "Access policies",
    "remove_role_tool_access": "Access policies",
    "set_role_tool_fallback_enabled": "Access policies",
    "set_role_category_default": "Access policies",
    "remove_role_category_default": "Access policies",
    "set_role_argument_constraint": "Access policies",
    "remove_role_argument_constraint": "Access policies",
    "search_audit_log": "Audit",
}


def create_tools_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/tools", response_model=list[ToolInfo])
    async def list_all_tools() -> list[ToolInfo]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        return [
            ToolInfo(
                upstream_id=t.upstream_id,
                original_name=t.original_name,
                prefixed_name=t.prefixed_name,
                description=t.description,
                input_schema=t.input_schema,
                title=t.title,
                output_schema=t.output_schema,
                annotations=ToolAnnotationsInfo(**t.annotations.model_dump()) if t.annotations else None,
            )
            for t in runtime.tool_registry.get_all_tools()
        ]

    @router.get("/admin-mcp/tools")
    async def get_admin_mcp_tools(
        _admin: str = Depends(deps.require_admin),
    ) -> list[dict[str, Any]]:
        if deps.list_admin_mcp_tools is None:
            return []
        tools = await deps.list_admin_mcp_tools()
        result: list[dict[str, Any]] = []
        for t in tools:
            entry: dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
                "category": _TOOL_CATEGORIES.get(t.name, "Other"),
            }
            if t.annotations:
                ann = t.annotations.model_dump(exclude_none=True)
                if ann:
                    entry["annotations"] = ann
            result.append(entry)
        return result

    return router
