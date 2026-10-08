"""Admin service-token router (3 routes).

- ``GET /service-tokens`` — list for the current org (never the hash,
  never the raw value).
- ``POST /service-tokens`` — mint; the raw token appears in this one
  response and nowhere else.
- ``DELETE /service-tokens/{label}`` — revoke (next gateway request
  with the token gets 401).

Service tokens deliberately do NOT enter ``config.users`` — they
never appear on the Team page and never count toward plan seats. The
role binding lives on the token registry; the gateway resolves it at
the auth boundary (see ``service_token_verifier``). Mint and revoke are
role actions (``RoleAdminService``): a token holds its role by name.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from mcpolis.domain.model.service_token import ServiceTokenRecord
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    ServiceTokenCreateRequest,
    ServiceTokenCreateResponse,
    ServiceTokenInfo,
)


def _info(record: ServiceTokenRecord) -> ServiceTokenInfo:
    return ServiceTokenInfo(
        label=record.label,
        role=record.role_name,
        created_by=record.created_by,
        created_at=record.created_at.isoformat(),
        last_used_at=(
            record.last_used_at.isoformat()
            if record.last_used_at is not None
            else None
        ),
    )


def create_service_tokens_admin_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-admin"],
        dependencies=[Depends(deps.require_admin)],
    )

    def _service() -> ServiceTokenService:
        if deps.service_token_service is None:
            raise HTTPException(500, "Service tokens are not configured")
        return deps.service_token_service

    @router.get("/service-tokens", response_model=list[ServiceTokenInfo])
    async def list_service_tokens() -> list[ServiceTokenInfo]:
        org_id = current_org_id.get()
        records = await _service().list_for_org(org_id)
        return [_info(r) for r in records]

    @router.post(
        "/service-tokens",
        response_model=ServiceTokenCreateResponse,
        status_code=201,
    )
    async def create_service_token(
        body: ServiceTokenCreateRequest,
        admin_email: str = Depends(deps.require_admin),
    ) -> ServiceTokenCreateResponse:
        minted = await deps.role_admin.mint_service_token(
            current_org_id.get(),
            label=body.label, role=body.role, actor=admin_email,
        )
        return ServiceTokenCreateResponse(
            token=minted.raw_token,
            info=_info(minted.record),
        )

    @router.delete("/service-tokens/{label}")
    async def revoke_service_token(
        label: str,
        admin_email: str = Depends(deps.require_admin),
    ) -> dict[str, str]:
        await deps.role_admin.revoke_service_token(
            current_org_id.get(), label, actor=admin_email,
        )
        return {"status": "revoked"}

    return router
