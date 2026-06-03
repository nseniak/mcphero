"""Per-MCP env-var management routes (admin-gated).

Two flavours of env var live here:

- ``is_secret=True`` (the default): plaintext is write-only. Accepted
  on PUT, never returned by GET / list. The list response carries
  ``last_four`` for display.
- ``is_secret=False``: value is returned in clear by GET / list so
  the UI can render it verbatim. The toggle is a **create-time
  decision** — replacing the value of an existing row preserves the
  original ``is_secret``.

Wire-shape parity with the existing upstream-admin router: every
endpoint is gated by ``deps.require_admin``, scoped to the current
org via ``current_org_id``, and returns clean 4xx errors for invalid
names instead of leaking pydantic-validator detail.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from mcpolis.domain.model.template_var import is_valid_template_var_name
from mcpolis.domain.services.system_variables import is_system_variable_name
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    TemplateVarSummaryView,
    SetTemplateVarRequest,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def create_template_vars_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-template-vars"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get(
        "/upstreams/{upstream_id}/template-vars",
        response_model=list[TemplateVarSummaryView],
    )
    async def list_template_vars(upstream_id: str) -> list[TemplateVarSummaryView]:
        org_id = current_org_id.get()
        summaries = await deps.template_var_repo.list_summaries(
            org_id, upstream_id,
        )
        return [
            TemplateVarSummaryView(
                name=s.name,
                is_secret=s.is_secret,
                value=s.value,
                last_four=s.last_four,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in summaries
        ]

    @router.put(
        "/upstreams/{upstream_id}/template-vars/{name}",
        response_model=TemplateVarSummaryView,
    )
    async def set_template_var(
        upstream_id: str, name: str, body: SetTemplateVarRequest,
    ) -> TemplateVarSummaryView:
        if not is_valid_template_var_name(name):
            raise HTTPException(
                400,
                f"Invalid variable name {name!r}: must match "
                "[A-Z_][A-Z0-9_]*",
            )
        # Reserved system-variable name (``HOME``, …) — user
        # Variables can't shadow them. Plan §C "Shared namespace,
        # enforced uniqueness".
        if is_system_variable_name(name):
            raise HTTPException(
                400,
                f"Cannot create a Variable named {name!r}: that name "
                "is reserved for the read-only system variable "
                f"${{{name}}}",
            )
        # Empty values are allowed: ``${EMPTY}`` substitutes to the
        # empty string, which is sometimes the intended value
        # (clearing a header, passing ``--flag ""``, etc.).
        org_id = current_org_id.get()
        # No collision check against Sandbox files — file names live
        # in their own namespace and don't participate in ``${...}``
        # substitution. A Variable and a file may share a name with
        # no functional consequence.
        # The repository enforces the "is_secret is immutable on
        # replace" contract — we just pass the body's flag through
        # and trust the repo to win on conflict.
        summary = await deps.template_var_repo.set(
            org_id, upstream_id, name, body.value,
            is_secret=body.is_secret,
        )
        return TemplateVarSummaryView(
            name=summary.name,
            is_secret=summary.is_secret,
            value=summary.value,
            last_four=summary.last_four,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )

    @router.delete("/upstreams/{upstream_id}/template-vars/{name}")
    async def delete_template_var(upstream_id: str, name: str) -> dict[str, str]:
        if not is_valid_template_var_name(name):
            raise HTTPException(
                400,
                f"Invalid variable name {name!r}: must match "
                "[A-Z_][A-Z0-9_]*",
            )
        org_id = current_org_id.get()
        await deps.template_var_repo.delete(org_id, upstream_id, name)
        return {"status": "deleted"}

    return router
