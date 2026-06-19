# pyright: reportUnusedFunction=false
# NOTE: no `from __future__ import annotations` — FastMCP tool registration
# uses issubclass() on annotations which breaks with stringified annotations.

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.org_service import OrgService

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True, idempotentHint=False)


def create_superadmin_mcp_server(
    org_repo: OrganizationRepository,
    runtime_manager: OrgRuntimeManager,
    org_service: OrgService,
) -> FastMCP:
    """Create the instance-level superadmin MCP server (cloud mode only).

    Provides cross-org management tools. Auth is handled by the caller
    (email allowlist middleware in app.py).

    Deletion routes through ``org_service.delete_organization`` — the
    single complete path shared with the dashboard, so the two can't
    drift on what they purge. ``org_repo`` / ``runtime_manager`` remain
    for the read tools and the dry-run preview.
    """
    server = FastMCP(name="MCP Hero Superadmin", streamable_http_path="/")

    @server.tool(annotations=READONLY)
    async def list_organizations() -> str:
        """List all organizations in this MCP Hero instance."""
        orgs = await org_repo.list_organizations()
        lines: list[str] = []
        for org in orgs:
            members = await org_repo.list_memberships(org.id)
            runtime = runtime_manager.get_cached(org.id)
            upstream_count = len(runtime.live_upstreams()) if runtime else 0
            lines.append(
                f"- {org.display_name} (slug={org.slug}, "
                f"members={len(members)}, upstreams={upstream_count}, "
                f"created={org.created_at.date().isoformat()})"
            )
        if not lines:
            return "No organizations found."
        return f"{len(lines)} organization(s):\n" + "\n".join(lines)

    @server.tool(annotations=READONLY)
    async def get_organization(slug: str) -> str:
        """Get details for an organization including its members."""
        org = await org_repo.get_by_slug(slug)
        if org is None:
            return f"Organization with slug '{slug}' not found."
        members = await org_repo.list_memberships(org.id)
        runtime = runtime_manager.get_cached(org.id)

        lines = [
            f"Organization: {org.display_name}",
            f"  Slug: {org.slug}",
            f"  ID: {org.id}",
            f"  Created: {org.created_at.isoformat()}",
            f"  Created by: {org.created_by_email or 'unknown'}",
            f"  Members ({len(members)}):",
        ]
        for m in members:
            lines.append(f"    - {m.email} (role={m.role})")

        if runtime:
            live = runtime.live_upstreams()
            lines.append(f"  Upstreams ({len(live)}):")
            for u in live:
                connected = runtime.client_manager.is_connected(u.id)
                status = "connected" if connected else "disconnected"
                lines.append(f"    - {u.id} ({status})")
        else:
            lines.append("  Runtime: not initialized")

        return "\n".join(lines)

    @server.tool(annotations=READONLY)
    async def system_status() -> str:
        """Show system-wide status: org count, runtime count, total upstreams."""
        orgs = await org_repo.list_organizations()
        runtimes = runtime_manager.all_runtimes
        total_upstreams = sum(len(r.live_upstreams()) for r in runtimes.values())
        # ``ready_upstream_ids`` rather than ``connected_upstream_ids``
        # so deferred-attach upstreams (cached, lazy reattach) count
        # as ready in the system tile — matches the user-facing
        # readiness pill on the org dashboard.
        total_connected = sum(
            len(r.client_manager.ready_upstream_ids)
            for r in runtimes.values()
        )
        return (
            f"Organizations: {len(orgs)}\n"
            f"Active runtimes: {len(runtimes)}\n"
            f"Total upstreams: {total_upstreams}\n"
            f"Connected upstreams: {total_connected}"
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def delete_organization(slug: str, confirm: bool = False) -> str:
        """Delete an organization and tear down its runtime.

        Pass confirm=true to actually delete. Without it, shows what
        would be deleted.
        """
        org = await org_repo.get_by_slug(slug)
        if org is None:
            return f"Organization with slug '{slug}' not found."

        members = await org_repo.list_memberships(org.id)
        runtime = runtime_manager.get_cached(org.id)
        upstream_count = len(runtime.live_upstreams()) if runtime else 0

        if not confirm:
            return (
                f"Would delete organization '{org.display_name}' "
                f"(slug={org.slug}):\n"
                f"  - {len(members)} member(s)\n"
                f"  - {upstream_count} upstream(s)\n"
                f"Pass confirm=true to proceed."
            )

        # Single complete delete path — tears down the runtime, removes
        # the org doc + memberships + service tokens, and purges every
        # org-scoped collection. Shared with the dashboard route.
        await org_service.delete_organization(org.id)

        logger.info(
            "superadmin.organization.deleted",
            org_slug=org.slug,
            org_id=org.id,
            member_count=len(members),
            upstream_count=upstream_count,
        )
        return f"Deleted organization '{org.display_name}' (slug={org.slug})."

    return server
