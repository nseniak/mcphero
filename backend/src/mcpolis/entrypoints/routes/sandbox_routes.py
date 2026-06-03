# pyright: reportUnusedFunction=false
"""Admin HTTP routes for the sandbox runtime.

One endpoint:

- ``GET /api/admin/sandbox/capabilities`` — the active provider's
  CPU/RAM/disk grid + capability flags. Drives the per-MCP
  resource picker on the upstream-detail edit form.

The per-upstream resource update used to live here as
``PUT /api/admin/upstreams/{id}/sandbox/resources``; it was folded
into the unified ``PUT /api/admin/upstreams/{id}`` body
(``sandbox_resources`` field) so the SETTINGS Save button commits
display-name + auth + JSON + env vars + resources atomically.

The Phase H lifecycle / sessions / kill-switch / sandbox-usage
routes were deleted along with the corresponding admin-UI
surfaces; see plan ``serene-beaming-tulip.md`` §Phase 4.
"""
from __future__ import annotations

from collections.abc import Callable

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.plan_policy import limits_for
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class SandboxResourceComboView(BaseModel):
    """Wire-shape mirror of :class:`SandboxResourceCombo`.

    ``enabled`` reflects whether the active org's plan permits this
    combo. The frontend always renders every combo and disables the
    options where ``enabled is False``, tagging them with a
    " — Team plan" suffix rather than hiding them.
    """

    cpu_vcpus: float
    memory_mb: int
    disk_gb: int
    enabled: bool = True


class SandboxCapabilitiesResponse(BaseModel):
    """JSON shape consumed by the admin UI's combined resource picker.

    Mirrors :class:`SandboxCapabilities` plus a ``provider`` echo so
    the UI can label which backend the constraints came from. The
    admin form renders one dropdown entry per
    :attr:`allowed_combinations` triple — guaranteeing every selectable
    value is supported by the active provider, including paired-grid
    backends (E2B) where the cross-product of the per-axis lists
    contains unsupported combos. The per-axis lists are still surfaced
    for callers that need them but no longer drive the picker.
    """

    provider: str
    allowed_cpu_vcpus: list[float]
    allowed_memory_mb: list[int]
    allowed_disk_gb: list[int]
    allowed_combinations: list[SandboxResourceComboView]
    supports_pause_resume: bool
    supports_egress_filtering: bool
    supports_persistent_disk: bool


def create_sandbox_router(
    *,
    require_admin: Callable[..., str],
    runtime_manager: OrgRuntimeManager,
    audit_repo: AuditRepository,
    event_bus: EventStream | None = None,
    org_repo: OrganizationRepository | None = None,
) -> APIRouter:
    # ``audit_repo`` and ``event_bus`` are kept on the signature for
    # forward-compat with future audited routes (and to keep the
    # call site stable across the Phase 4 cleanup); they aren't
    # consumed by the surviving endpoint.
    _ = audit_repo, event_bus
    router = APIRouter(
        prefix="/api/admin", tags=["sandbox"],
        dependencies=[Depends(require_admin)],
    )

    @router.get(
        "/sandbox/capabilities",
        response_model=SandboxCapabilitiesResponse,
    )
    async def get_sandbox_capabilities() -> SandboxCapabilitiesResponse:
        """Return the active provider's CPU/RAM/disk grid.

        Drives the admin UI's per-MCP resource picker: the form is
        provider-aware, so changing ``MCPOLIS_SANDBOX_PROVIDER``
        flips the available combinations the next time an admin
        loads the page.
        """
        org_id = current_org_id.get()
        runtime = await runtime_manager.get(org_id)
        caps = await runtime.client_manager.get_active_capabilities()
        plan: PlanName = PlanName.free
        if org_repo is not None:
            org = await org_repo.get_organization(org_id)
            if org is not None:
                plan = org.subscription.plan
        plan_limits = limits_for(plan)
        allowed_combos = plan_limits.allowed_sandbox_combos
        return SandboxCapabilitiesResponse(
            provider=caps.provider,
            allowed_cpu_vcpus=list(caps.allowed_cpu_vcpus),
            allowed_memory_mb=list(caps.allowed_memory_mb),
            allowed_disk_gb=list(caps.allowed_disk_gb),
            allowed_combinations=[
                SandboxResourceComboView(
                    cpu_vcpus=combo.cpu_vcpus,
                    memory_mb=combo.memory_mb,
                    disk_gb=combo.disk_gb,
                    enabled=(
                        allowed_combos is None
                        or (int(combo.cpu_vcpus), combo.memory_mb)
                        in allowed_combos
                    ),
                )
                for combo in caps.allowed_combinations
            ],
            supports_pause_resume=caps.supports_pause_resume,
            supports_egress_filtering=caps.supports_egress_filtering,
            supports_persistent_disk=caps.supports_persistent_disk,
        )

    return router


__all__ = [
    "SandboxCapabilitiesResponse",
    "SandboxResourceComboView",
    "create_sandbox_router",
]
