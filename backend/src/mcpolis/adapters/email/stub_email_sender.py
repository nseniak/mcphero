"""Stub ``EmailSender`` that records + logs without sending.

Default implementation while the §5.2 pipeline is behind the
``MCPOLIS_UPSTREAM_HEALTH_EMAIL_ENABLED`` feature flag. Records a
list of sent messages so tests can assert on the body / recipient
without a network dependency; also logs each message at INFO so an
operator running in standalone mode can eyeball the notification
flow without a real mail provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import structlog


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class SentEmail:
    to: str
    subject: str
    body_text: str
    body_html: str | None


@dataclass
class StubEmailSender:
    sent: list[SentEmail] = field(default_factory=lambda: [])

    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        self.sent.append(SentEmail(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        ))
        logger.info(
            "email.stub.sent",
            to=to,
            subject=subject,
            body_length=len(body_text),
        )
