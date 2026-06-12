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
the auth boundary (see ``service_token_verifier``).
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.domain.model.service_token import ServiceTokenRecord
from mcpolis.domain.ports.service_token_repository import (
    DuplicateServiceTokenLabelError,
)
from mcpolis.domain.services.service_token_service import ServiceTokenService
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    ServiceTokenCreateRequest,
    ServiceTokenCreateResponse,
    ServiceTokenInfo,
)

# Lowercase alphanumerics, hyphen, underscore; must start with an
# alphanumeric; max 64 chars. Keeps ``svc:<label>`` clean in audit
# rows, log context, and filter UIs.
LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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
        org_id = current_org_id.get()
        if not LABEL_PATTERN.match(body.label):
            raise HTTPException(
                400,
                "Label must be 1-64 chars of lowercase letters, digits, "
                "'-' or '_', starting with a letter or digit",
            )
        runtime = await deps.runtime_manager.get(org_id)
        if body.role not in runtime.policy_engine.config.roles:
            raise HTTPException(400, f"Role '{body.role}' not found")
        try:
            minted = await _service().mint(
                org_id=org_id,
                label=body.label,
                role_name=body.role,
                created_by=admin_email,
            )
        except DuplicateServiceTokenLabelError:
            raise HTTPException(
                409, f"Service token '{body.label}' already exists",
            ) from None
        get_analytics().track_async(
            admin_email,
            "service_token_created",
            {"label": body.label, "role": body.role},
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
        org_id = current_org_id.get()
        revoked = await _service().revoke(org_id, label)
        if not revoked:
            raise HTTPException(404, f"Service token '{label}' not found")
        get_analytics().track_async(
            admin_email,
            "service_token_revoked",
            {"label": label},
        )
        return {"status": "revoked"}

    return router
