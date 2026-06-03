"""§5.2 — proactive upstream health check + email notification.

Motivation (``internal/documents/oauth-durability.md`` §5.2): when an upstream
refresh genuinely dies (§3.4 revocation), the user first learns
about it by a tool call failing in Claude, mid-task. That's the
surprise we want to avoid. This module wraps the notification
pipeline:

1. For each ``(org, upstream, user)`` with a recorded refresh-failure
   signature (captured by §5.4 and persisted per-user by §5.1's
   storage extension), check whether the signature's
   ``error_code == "invalid_grant"`` — the RFC 6749 §5.2 signal that
   the refresh token is genuinely dead and no amount of retries will
   revive it.
2. For ``admin_oauth`` upstreams, notify every org admin (role
   ``admin``) via membership lookup; for ``per_user_oauth`` upstreams,
   notify the single affected user (``user_id`` is already an email).
3. Sign a re-auth link using the HMAC token pattern from the OAuth
   state tokens — same key, 7-day expiry — so the user can land
   directly on ``/my-tools`` without an intermediate login.
4. Mark ``(upstream, user)`` as notified so the next hourly tick
   doesn't re-send. The ``clear_notified`` call in the successful-
   refresh paths resets this so a future genuine failure triggers a
   new notification.

The ``EmailSender`` dependency is a port; the ``StubEmailSender``
stub ships by default so the pipeline is testable and visible in
logs without a real mail provider. The real adapter (Postmark /
SES / Resend / SMTP) lands in a follow-up.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from mcpolis.adapters.auth.hmac_token import sign_token
from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import ADMIN_USER_ID
from mcpolis.domain.ports.email_sender import EmailSender


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

HEALTH_EMAIL_INTERVAL = 60 * 60  # 1 hour
REAUTH_LINK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


# Resolver: given ``org_id`` and ``user_id``, return the email
# address(es) to notify. ``ADMIN_USER_ID`` is expected to fan out to
# every org admin; a real user_id resolves to itself (it's already an
# email). Pulled out as a protocol-shaped callable so the health-check
# module doesn't grow a direct dependency on ``OrganizationRepository``.
AdminEmailResolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class NotificationDecision:
    """Return shape for the pure decision function. Both the decide
    path and the test assertions read ``should_notify`` plus the
    resolved recipient list without a second storage read."""
    should_notify: bool
    reason: str


def decide_notification(
    *,
    signature: dict[str, object] | None,
    already_notified: bool,
) -> NotificationDecision:
    """Pure policy: when does a refresh-failure state warrant an email?

    - No recorded signature → nothing known-broken → no notify.
    - ``error_code != "invalid_grant"`` → transient or unknown root
      cause; §5.1 will retry. Notifying here would spam users over
      upstream 5xx bursts.
    - Already notified → stay quiet until ``clear_notified`` runs
      (i.e. a successful refresh has cleared the state).
    - Otherwise → notify.
    """
    if signature is None:
        return NotificationDecision(False, "no signature recorded")
    error_code = signature.get("error_code")
    if error_code != "invalid_grant":
        return NotificationDecision(
            False, f"error_code={error_code!r} is not invalid_grant",
        )
    if already_notified:
        return NotificationDecision(False, "already notified")
    return NotificationDecision(True, "invalid_grant")


def build_reauth_link(
    *,
    server_url: str,
    org_id: str,
    upstream_id: str,
    user_id: str,
    key: bytes,
    now: float | None = None,
) -> str:
    """Sign a short-lived re-auth link that lands on ``/my-tools``.

    Payload mirrors the existing OAuth state token shape: ``iat``
    (issued-at) plus the routing fields. Verification runs through
    the same ``verify_token`` helper — reuse, not parallel crypto.
    """
    issued_at = now if now is not None else time.time()
    payload: dict[str, object] = {
        "iat": issued_at,
        "kind": "upstream_reauth",
        "org_id": org_id,
        "upstream_id": upstream_id,
        "user_id": user_id,
    }
    token = sign_token(payload, key)
    return f"{server_url.rstrip('/')}/my-tools?reauth={token}"


def _render_email(
    *,
    upstream: UpstreamDefinition,
    reauth_link: str,
) -> tuple[str, str]:
    """Simple inline body — no template engine for the first cut.
    Returns (subject, body_text). HTML variant left for a follow-up
    once ops decides on branding / footer."""
    subject = (
        f"Reconnect your {upstream.display_name} integration on MCP Hero"
    )
    body = (
        f"Your {upstream.display_name} connection in MCP Hero needs "
        f"to be re-authenticated.\n"
        f"\n"
        f"Click the link below to sign in again and restore access:\n"
        f"{reauth_link}\n"
        f"\n"
        f"If you didn't expect this email, it's safe to ignore — no "
        f"action is taken until you click the link.\n"
        f"\n"
        f"— MCP Hero (published by Nitsan Seniak)\n"
    )
    return subject, body


async def _notify_one_recipient(
    *,
    recipient: str,
    upstream: UpstreamDefinition,
    org_id: str,
    user_id: str,
    server_url: str,
    hmac_key: bytes,
    email_sender: EmailSender,
) -> None:
    link = build_reauth_link(
        server_url=server_url,
        org_id=org_id,
        upstream_id=upstream.id,
        user_id=user_id,
        key=hmac_key,
    )
    subject, body = _render_email(upstream=upstream, reauth_link=link)
    await email_sender.send_email(
        to=recipient, subject=subject, body_text=body,
    )


async def check_and_notify_upstream(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    connection_store: ConnectionStore,
    email_sender: EmailSender,
    admin_email_resolver: AdminEmailResolver,
    server_url: str,
    hmac_key: bytes,
) -> bool:
    """Inspect one ``(upstream, user)`` pair. Send email(s) if the
    recorded signature is ``invalid_grant`` AND we haven't already
    notified. Returns True iff at least one email was sent.

    Recipient resolution:

    - ``admin_oauth`` upstream → fan out to every org admin via
      ``admin_email_resolver(org_id)``.
    - ``per_user_oauth`` upstream → ``user_id`` is the email; send
      directly.

    Mark-as-notified is keyed per ``(upstream, user_id)`` — for
    ``admin_oauth`` that's ``(upstream, __admin__)``. So if an org
    has three admins, they all get the email at most once per
    failure cycle.
    """
    signature = await connection_store.get_refresh_failure_signature(
        org_id, upstream.id, user_id,
    )
    already = await connection_store.was_notified(
        org_id, upstream.id, user_id,
    )
    decision = decide_notification(
        signature=signature, already_notified=already,
    )
    if not decision.should_notify:
        logger.debug(
            "upstream.health.notify.skipped",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
            reason=decision.reason,
        )
        return False

    if upstream.auth.mode == AuthMode.admin_oauth:
        recipients = await admin_email_resolver(org_id)
    elif upstream.auth.mode == AuthMode.per_user_oauth:
        recipients = [user_id]
    else:
        # Service-account upstreams don't have OAuth state to re-auth.
        logger.debug(
            "upstream.health.notify.skipped.no_reauth_flow",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
            auth_mode=upstream.auth.mode.value,
        )
        return False

    if not recipients:
        logger.warning(
            "upstream.health.notify.no_recipients",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
        )
        return False

    sent_any = False
    for recipient in recipients:
        try:
            await _notify_one_recipient(
                recipient=recipient,
                upstream=upstream,
                org_id=org_id,
                user_id=user_id,
                server_url=server_url,
                hmac_key=hmac_key,
                email_sender=email_sender,
            )
            sent_any = True
        except Exception:
            logger.exception(
                "upstream.health.notify.failed",
                org_id=org_id,
                upstream_id=upstream.id,
                user=user_id,
                recipient=recipient,
            )

    if sent_any:
        await connection_store.mark_notified(
            org_id, upstream.id, user_id,
        )
        logger.info(
            "upstream.health.notify.sent",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
            recipient_count=len(recipients),
        )
    return sent_any


async def run_health_check_for_org(
    *,
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    email_sender: EmailSender,
    admin_email_resolver: AdminEmailResolver,
    server_url: str,
    hmac_key: bytes,
) -> None:
    """One pass: iterate every stored token for this org and call
    ``check_and_notify_upstream``.

    Walks the full stored-token list (rather than live sessions)
    because a user whose refresh died may have no current session —
    the whole point of §5.2 is to reach those people before they
    open Claude. The "when to run" concern (tick cadence + feature
    flag) stays in the caller (``app.py``'s lifespan); this function
    does one pass and returns.
    """
    upstream_by_id = {u.id: u for u in upstreams}
    oauth_ids = {
        u.id for u in upstreams
        if u.auth.mode in (AuthMode.admin_oauth, AuthMode.per_user_oauth)
    }
    all_tokens = await connection_store.get_all_stored_tokens(org_id)
    for upstream_id, user_id in all_tokens:
        if upstream_id not in oauth_ids:
            continue
        upstream = upstream_by_id.get(upstream_id)
        if upstream is None:
            continue
        # admin_oauth upstreams are always stored under ADMIN_USER_ID
        # — skip any stray user rows that the admin-mode loop
        # shouldn't own.
        if (
            upstream.auth.mode == AuthMode.admin_oauth
            and user_id != ADMIN_USER_ID
        ):
            continue
        try:
            await check_and_notify_upstream(
                org_id=org_id,
                upstream=upstream,
                user_id=user_id,
                connection_store=connection_store,
                email_sender=email_sender,
                admin_email_resolver=admin_email_resolver,
                server_url=server_url,
                hmac_key=hmac_key,
            )
        except Exception:
            logger.exception(
                "upstream.health.iteration.failed",
                org_id=org_id,
                upstream_id=upstream_id,
                user=user_id,
            )
