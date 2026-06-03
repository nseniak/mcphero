# pyright: reportUnusedFunction=false
"""Client-side error / event ingest.

Frontend code (window.onerror, unhandledrejection, and the
``reportClientError`` helper) POSTs structured payloads here. We
log them via structlog so they ride the existing Vector → Elastic
pipeline and surface as ``client.<event>`` records in Discover —
the same shape backend events use.

Why open: errors can fire before login completes (e.g. during the
SignupPage flow that prompted this endpoint), so a hard auth gate
would lose the exact records we most need to see. We bound abuse by:

* truncating the payload (max ``MAX_BODY_BYTES``);
* clamping each free-text field to ``MAX_FIELD_LEN``;
* leaving the existing Vector throttle (5000/min/event-name) as the
  cost backstop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Body cap large enough to accommodate the documented 4x stack budget
# (MAX_FIELD_LEN * 4 = 4 KiB) plus the other fields and JSON overhead.
# Realistic JS stack traces in production run 1-3 KiB; with source-maps
# they routinely hit 4 KiB. The Vector throttle is the actual cost
# backstop, so this bound only needs to keep a single record from
# being unbounded.
MAX_BODY_BYTES = 8192
MAX_FIELD_LEN = 1024


def _clip(value: str | None, limit: int = MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "...[clipped]"


class ClientErrorReport(BaseModel):
    """Shape of the JSON body the frontend POSTs."""

    event: Literal[
        "client.unhandled_error",
        "client.unhandled_rejection",
        "client.reported_error",
    ] = Field(description="Structured event name.")
    message: str = Field(default="", description="Error message.")
    stack: str | None = Field(default=None, description="Stack trace.")
    url: str | None = Field(default=None, description="window.location.href.")
    source: str | None = Field(
        default=None, description="Source file (window.onerror arg)."
    )
    line: int | None = Field(default=None)
    column: int | None = Field(default=None)
    release: str | None = Field(default=None)
    app_environment: str | None = Field(default=None)


def create_client_errors_router(
    get_optional_user: Callable[[Request], str | None],
) -> APIRouter:
    router = APIRouter(prefix="/api/client-errors", tags=["client-errors"])

    @router.post("", status_code=204, response_model=None)
    async def ingest(request: Request) -> None:
        # Read raw bytes first to enforce a hard cap before parsing —
        # pydantic would happily allocate a multi-MB string.
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            report = ClientErrorReport.model_validate_json(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        user_email = get_optional_user(request)
        # Use the client-provided event name as the structlog event so
        # Kibana facets match the convention backend events follow
        # (dot-separated namespace, low-cardinality bucket).
        logger.warning(
            report.event,
            message=_clip(report.message),
            stack=_clip(report.stack, limit=MAX_FIELD_LEN * 4),
            url=_clip(report.url),
            source=_clip(report.source),
            line=report.line,
            column=report.column,
            release=_clip(report.release, limit=128),
            app_environment=_clip(report.app_environment, limit=64),
            user_agent=_clip(request.headers.get("user-agent"), limit=512),
            user_email=user_email,
        )

    return router
