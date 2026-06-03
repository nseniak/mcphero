"""Per-MCP **Sandbox files** management routes (admin-gated).

Mirrors the per-concern shape of ``template_vars.py``: every endpoint
is gated by ``deps.require_admin``, scoped to the current org via
``current_org_id``, and rejects invalid names with a clean 400
instead of leaking pydantic-validator detail.

File names live in their **own** namespace (no longer overloaded
into ``${...}`` substitution). ``target_path`` accepts the same
``${...}`` references env-var values accept (system + user vars);
operators wire a Variable to a file's path by typing the
``${HOME}/...`` literal on both sides rather than via a special
``${FILE_NAME}`` token. The only reserved-name check that remains
is system-variable shadowing, which is enforced on user Variables;
file names are unconstrained beyond the regex.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from mcpolis.domain.model.sandbox_file import (
    MAX_FILE_BYTES,
    is_valid_sandbox_file_name,
)
from mcpolis.domain.model.upstream import TransportType
from mcpolis.domain.services.system_variables import (
    system_variables_for_sandbox,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.routes.dashboard._deps import DashboardDeps
from mcpolis.entrypoints.routes.dashboard._models import (
    SandboxFileSummaryView,
    SetSandboxFileRequest,
    SystemVariableView,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _check_target_path(target_path: str) -> None:
    """Lightweight shape check on ``target_path``.

    The token grammar (``${[A-Z_][A-Z0-9_]*}``) is enforced by the
    pydantic ``SandboxFile.target_path`` validator and the runtime
    substitutor. We only verify here that the path is rooted —
    either an absolute literal or a path that begins with a
    ``${...}`` (which the substitutor will resolve at launch).
    Token resolution itself is deferred to launch time so a path
    like ``${HOME}/.config/${PROFILE}/creds`` is accepted as long
    as ``HOME`` and ``PROFILE`` resolve when the sandbox starts.
    """
    if not (target_path.startswith("/") or target_path.startswith("${")):
        raise HTTPException(
            400,
            "target_path must be absolute or start with a "
            "${...} reference (e.g. ${HOME}/.config/...)",
        )


def create_sandbox_files_router(deps: DashboardDeps) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin", tags=["dashboard-sandbox-files"],
        dependencies=[Depends(deps.require_admin)],
    )

    @router.get(
        "/upstreams/{upstream_id}/sandbox-files",
        response_model=list[SandboxFileSummaryView],
    )
    async def list_sandbox_files(
        upstream_id: str,
    ) -> list[SandboxFileSummaryView]:
        if deps.sandbox_file_repo is None:
            return []
        org_id = current_org_id.get()
        summaries = await deps.sandbox_file_repo.list_summaries(
            org_id, upstream_id,
        )
        return [
            SandboxFileSummaryView(
                name=s.name,
                display_name=s.display_name,
                target_path=s.target_path,
                sha256=s.sha256,
                size_bytes=s.size_bytes,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in summaries
        ]

    @router.put(
        "/upstreams/{upstream_id}/sandbox-files/{name}",
        response_model=SandboxFileSummaryView,
    )
    async def set_sandbox_file(
        upstream_id: str, name: str, body: SetSandboxFileRequest,
    ) -> SandboxFileSummaryView:
        if deps.sandbox_file_repo is None:
            raise HTTPException(503, "sandbox files repository not configured")
        if not is_valid_sandbox_file_name(name):
            raise HTTPException(
                400,
                f"Invalid sandbox file id {name!r}: 1-64 characters, "
                "alphanumeric / dash / dot / underscore only",
            )
        size_bytes = len(body.contents.encode("utf-8"))
        if size_bytes > MAX_FILE_BYTES:
            raise HTTPException(
                400,
                f"Sandbox file too large: {size_bytes} bytes exceeds "
                f"{MAX_FILE_BYTES} byte cap",
            )
        _check_target_path(body.target_path)

        # File names live in their own namespace; no collision check
        # against user Variables. A user Variable named ``GCP_CRED``
        # and a Sandbox file named ``GCP_CRED`` are independent —
        # ``${GCP_CRED}`` resolves to the Variable's value at every
        # substitution site (env, args, command, headers, url,
        # target_path) and the file's path is whatever target_path
        # resolves to.
        org_id = current_org_id.get()

        summary = await deps.sandbox_file_repo.set(
            org_id, upstream_id, name, body.contents, body.target_path,
            display_name=body.display_name,
        )
        return SandboxFileSummaryView(
            name=summary.name,
            display_name=summary.display_name,
            target_path=summary.target_path,
            sha256=summary.sha256,
            size_bytes=summary.size_bytes,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )

    @router.delete(
        "/upstreams/{upstream_id}/sandbox-files/{name}",
    )
    async def delete_sandbox_file(
        upstream_id: str, name: str,
    ) -> dict[str, str]:
        if deps.sandbox_file_repo is None:
            return {"status": "deleted"}
        if not is_valid_sandbox_file_name(name):
            raise HTTPException(
                400,
                f"Invalid sandbox file id {name!r}: 1-64 characters, "
                "alphanumeric / dash / dot / underscore only",
            )
        org_id = current_org_id.get()
        await deps.sandbox_file_repo.delete(org_id, upstream_id, name)
        return {"status": "deleted"}

    @router.get(
        "/system-variables",
        response_model=list[SystemVariableView],
    )
    async def list_system_variables(
        transport: TransportType,
    ) -> list[SystemVariableView]:
        # System Variables only make sense for transports that run
        # inside a sandbox — HTTP upstreams have no filesystem to
        # parameterize. Keep the response shape consistent: empty
        # list rather than a 4xx, so the wizard can poll on transport
        # changes without flicker.
        if transport != TransportType.stdio:
            return []
        return [
            SystemVariableView(name=name, value=value)
            for name, value in sorted(
                system_variables_for_sandbox().items(),
            )
        ]

    return router
