"""User-facing /my-tools listing (1 route).

Different concern from the admin-tab listing — this surfaces only the
upstreams the caller's role allows them to see, with per-tool
filtering and the per-viewer ``user_connection_status`` (only
meaningful for ``per_user_oauth``).
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends

from mcpolis.domain.model.policy import AuthMode
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import (
    DashboardDeps,
    resolve_upstream_readiness,
)
from mcpolis.entrypoints.routes.dashboard._models import (
    UserMcpInfo,
    UserToolSummary,
)


def create_upstream_user_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(prefix="/api/user", tags=["dashboard-user"])

    @router.get("/mcps", response_model=list[UserMcpInfo])
    async def user_mcps(
        email: str = Depends(deps.get_current_user),
    ) -> list[UserMcpInfo]:
        org_id = current_org_id.get()
        runtime = await deps.runtime_manager.get(org_id)
        upstreams = await runtime.config_service.list_upstreams(org_id)
        allowed = runtime.policy_engine.get_allowed_upstreams(email)

        all_tools = runtime.tool_registry.get_all_tools()
        # Apply tool-level policy filtering
        tool_triples = [
            (t.upstream_id, t.original_name,
             t.annotations.to_flags() if t.annotations else {})
            for t in all_tools
        ]
        allowed_tools = runtime.policy_engine.filter_tools(email, tool_triples)
        allowed_tool_keys = {(uid, name) for uid, name, _ in allowed_tools}

        tools_by_upstream: dict[str, list[UserToolSummary]] = {}
        for t in all_tools:
            if (t.upstream_id, t.original_name) in allowed_tool_keys:
                tools_by_upstream.setdefault(t.upstream_id, []).append(
                    UserToolSummary(
                        name=t.original_name,
                        description=t.description,
                    ),
                )

        results: list[UserMcpInfo] = []
        for u in upstreams:
            if u.id not in allowed:
                continue

            # ``ready`` is the org-level "an admin has authenticated /
            # the upstream is operational" gate. /my-tools maps it to
            # the Unavailable card state when false. The per-viewer
            # signed-in state is reported separately as
            # ``user_connection_status`` and only meaningful for
            # ``per_user_oauth``.
            ready, _ = await resolve_upstream_readiness(
                u, org_id, deps.connection_store, runtime,
            )
            if u.auth.mode == AuthMode.per_user_oauth:
                has_my_token = False
                if deps.connection_store is not None:
                    token = await deps.connection_store.get_user_token(
                        org_id, email, u.id,
                    )
                    has_my_token = token is not None
                user_conn_status = (
                    "connected" if has_my_token else "not_connected"
                )
            elif u.auth.mode == AuthMode.admin_oauth:
                user_conn_status = "connected" if ready else "not_connected"
            else:
                user_conn_status = "connected" if ready else "not_connected"

            upstream_tools = tools_by_upstream.get(u.id, [])
            results.append(
                UserMcpInfo(
                    id=u.id,
                    display_name=u.display_name,
                    transport=u.transport.value,
                    auth_mode=u.auth.mode.value,
                    ready=ready,
                    url=u.http.url if u.http else None,
                    user_connection_status=user_conn_status,
                    tool_count=len(upstream_tools),
                    tools=upstream_tools,
                ),
            )
        return results

    return router
