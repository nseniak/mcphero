"""Real ``EmailSender`` that delivers via SMTP submission.

Built for Google Workspace's submission endpoint
(``smtp.gmail.com:587``, STARTTLS) authenticated with an App Password,
but works against any SMTP server. Relaying through Workspace is the
deliverability win: Google already publishes SPF / DKIM / DMARC for
the sending domain, so mail aligns automatically — no separate
domain reputation to warm up.

The authenticating user (``username``) and the visible ``From``
address (``from_addr``) are deliberately separate. ``info@mcphero.io``
is a verified "Send mail as" alias on an underlying Workspace user,
so we SMTP-auth as that user and stamp the alias on the From header.

Contract (``EmailSender`` port): raise on a failed send. The §5.2
notifier only calls ``mark_notified`` after a successful send, so a
raised exception here means the admin gets retried on the next hourly
sweep rather than being silently marked done.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import EmailMessage

import aiosmtplib
import structlog


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Injection seam for tests: matches the subset of ``aiosmtplib.send``
# we call. Defaults to the real client; unit tests pass a fake that
# records the message + connection kwargs without touching a network.
SendFn = Callable[..., Awaitable[object]]


@dataclass
class SmtpEmailSender:
    host: str
    port: int
    username: str
    password: str
    from_addr: str
    from_name: str = "MCP Hero"
    send_fn: SendFn = aiosmtplib.send

    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        message = self._build_message(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
        # Port 465 = implicit TLS from the first byte; everything else
        # (587 submission) = plaintext connect then STARTTLS upgrade.
        # aiosmtplib rejects both being set, so they're mutually
        # exclusive here.
        use_tls = self.port == 465
        await self.send_fn(
            message,
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_tls=use_tls,
            start_tls=not use_tls,
        )
        logger.info(
            "email.smtp.sent",
            to=to,
            subject=subject,
            body_length=len(body_text),
            has_html=body_html is not None,
        )

    def _build_message(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = (
            f"{self.from_name} <{self.from_addr}>"
            if self.from_name
            else self.from_addr
        )
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_text)
        if body_html is not None:
            # Promotes the message to multipart/alternative: clients
            # that render HTML pick the rich part, the rest fall back
            # to the plain-text body set above.
            message.add_alternative(body_html, subtype="html")
        return message
