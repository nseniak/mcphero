"""Admin user-management router (4 routes).

- ``GET /users`` — list (with active/pending status).
- ``POST /users`` — pre-approve a user (no membership row yet).
- ``DELETE /users/{email}`` — full teardown (config + membership +
  gateway tokens + upstream sessions + per-user OAuth rows).
- ``PUT /users/{email}/role`` — change role + propagate to membership.

Each route runs the teammate action the Admin MCP shares
(``UserAdminService``); a refusal becomes an HTTP error through the
app-level ``AdminActionRefused`` handler.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends

from mcpolis.domain.services.user_admin_service import UserView
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    AddUserRequest,
    SetRoleRequest,
    UserInfo,
)


def _user_info(view: UserView) -> UserInfo:
    return UserInfo(
        email=view.email,
        role=view.role,
        is_admin=view.is_admin,
        status=view.status,
    )


def create_users_admin_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get("/users", response_model=list[UserInfo])
    async def list_users() -> list[UserInfo]:
        views = await deps.user_admin.list_users(current_org_id.get())
        return [_user_info(view) for view in views]

    @router.post("/users", response_model=UserInfo, status_code=201)
    async def add_user(
        body: AddUserRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UserInfo:
        view = await deps.user_admin.add_user(
            current_org_id.get(), body.email, body.role,
            actor=admin_email, source="dashboard.add_user",
        )
        return _user_info(view)

    @router.delete("/users/{email}")
    async def remove_user(
        email: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        await deps.user_admin.remove_user(
            current_org_id.get(), email, actor=admin_email,
        )
        return {"status": "removed"}

    @router.put("/users/{email}/role")
    async def set_user_role(
        email: str,
        body: SetRoleRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> UserInfo:
        view = await deps.user_admin.set_user_role(
            current_org_id.get(), email, body.role, actor=admin_email,
        )
        return _user_info(view)

    return router
