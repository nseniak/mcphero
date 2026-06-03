"""Outbound email port.

Introduced by §5.2 of ``internal/documents/oauth-durability.md`` as the
dependency the ``upstream_health_check`` notifier depends on. No
provider is wired in yet — ``StubEmailSender`` in
``adapters/email/stub_email_sender.py`` satisfies the contract by
logging the rendered message, which is what ships when
``MCPOLIS_UPSTREAM_HEALTH_EMAIL_ENABLED`` is False (the default).
A real adapter (Postmark, SES, Resend, Gmail/SMTP) lands in a
follow-up once the operator has chosen a provider and proved
deliverability; at that point the stub is swapped in DI, nothing
else in the app changes.
"""
from __future__ import annotations

from typing import Protocol


class EmailSender(Protocol):
    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        """Deliver a single transactional email.

        Implementations must raise on permanent errors (bad address,
        provider refusal) so the caller records it; transient retries
        are the adapter's responsibility.
        """
        ...
