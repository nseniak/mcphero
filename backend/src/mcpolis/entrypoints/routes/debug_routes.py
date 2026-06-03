# pyright: reportUnusedFunction=false
"""Debug routes for verifying observability wiring.

Always registered, but each route is gated on the caller's email
being in ``MCPOLIS_SUPERADMIN_EMAILS``. Non-superadmins get 404, so
the routes look like they don't exist (matches the /__debug frontend
page's behavior). Useful for end-to-end smoke-testing Sentry +
Mixpanel from a deployed instance.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mcpolis.adapters.observability.analytics_client import AnalyticsClient
from mcpolis.domain.ports.email_sender import EmailSender


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class TestEmailRequest(BaseModel):
    to: str


def create_debug_router(
    analytics: AnalyticsClient,
    email_sender: EmailSender,
    get_current_user: Callable[..., str],
    superadmin_emails: set[str],
) -> APIRouter:
    router = APIRouter(prefix="/api/debug", tags=["debug"])

    async def require_superadmin(
        email: str = Depends(get_current_user),
    ) -> str:
        if email not in superadmin_emails:
            # 404 (not 403) so the routes are invisible to non-admins.
            raise HTTPException(status_code=404)
        return email

    @router.get("/sentry-error")
    async def trigger_sentry_error(
        _email: str = Depends(require_superadmin),
    ) -> None:
        # Intentionally unhandled so FastAPI's Sentry integration captures it.
        raise RuntimeError("sentry smoke test (backend)")

    @router.get("/http-error")
    async def trigger_http_error(
        _email: str = Depends(require_superadmin),
    ) -> None:
        # HTTPException is expected flow — Sentry should NOT capture it.
        raise HTTPException(status_code=418, detail="teapot (expected, not captured)")

    @router.post("/mixpanel-event")
    async def trigger_mixpanel_event(
        email: str = Depends(require_superadmin),
    ) -> dict[str, bool | str]:
        """Fire a smoke-test Mixpanel event from the backend."""
        analytics.track_async(
            email,
            "debug_backend_event",
            {"source": "debug_page"},
        )
        return {"analytics_enabled": analytics.enabled, "event": "debug_backend_event"}

    @router.post("/test-email")
    async def trigger_test_email(
        body: TestEmailRequest,
        email: str = Depends(require_superadmin),
    ) -> dict[str, str | bool]:
        """Send a one-off test email through the wired ``EmailSender``.

        ``transport`` tells the operator whether a real message went
        out (``SmtpEmailSender``) or the call was a logging no-op
        (``StubEmailSender`` — when ``MCPOLIS_SMTP_HOST`` is unset), so
        a "success" with the stub isn't mistaken for real delivery.
        """
        recipient = body.to.strip()
        if "@" not in recipient:
            raise HTTPException(
                status_code=400,
                detail="A valid recipient email address is required.",
            )
        transport = type(email_sender).__name__
        try:
            await email_sender.send_email(
                to=recipient,
                subject="MCP Hero test email",
                body_text=(
                    "This is a test email from MCP Hero, sent from the "
                    "super-admin observability page.\n\n"
                    "If it reached your inbox, transactional email "
                    "delivery is working.\n\n"
                    "— MCP Hero (published by Nitsan Seniak)\n"
                ),
            )
        except Exception as exc:
            logger.exception(
                "debug.test_email.failed",
                to=recipient,
                transport=transport,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Send failed via {transport}: {exc}",
            ) from exc
        logger.info(
            "debug.test_email.sent",
            to=recipient,
            transport=transport,
            triggered_by=email,
        )
        return {
            "sent": True,
            "to": recipient,
            "transport": transport,
            "real_delivery": transport != "StubEmailSender",
        }

    return router
