"""§5.2 — tell people when they must sign in to an upstream again.

Motivation (``internal/documents/oauth-durability.md`` §5.2): when an upstream
refresh genuinely dies (§3.4 revocation), the user first learns
about it by a tool call failing in Claude, mid-task. That's the
surprise we want to avoid. This module wraps the notification
pipeline:

1. When:
   - Right after the gateway deletes a sign-in the upstream refused
     with a terminal code (``SignInWarner.warn_deleted``, scheduled by
     ``delete_refused_sign_in``). Both places that delete one, the
     periodic refresh and a reconnect, go through that funnel, so no
     member is signed out silently. The email is sent in the
     background: a reconnect, and every request joined to it, never
     waits for the mail server.
   - The hourly sweep (``run_health_check_for_org``): a sign-in still
     stored whose last refresh was refused with a terminal code
     (``TERMINAL_AUTH_ERROR_CODES``: ``invalid_grant`` = the refresh
     token is dead, ``invalid_client`` = the app registration is
     dead). ``(upstream, user)`` is then marked notified so the next
     tick doesn't re-send; ``clear_notified`` in the success paths
     resets it so a future genuine failure triggers a new email.
2. Who:
   - ``admin_oauth``: every org admin. After a deletion, only when no
     admin sign-in is left for the upstream: while one is left the
     org's shared sign-in still works, and nobody needs to act.
   - ``per_user_oauth``: the member whose sign-in it is.
3. Where the link goes: the page where the recipient can sign in
   again. The MCP server's admin page for an admin sign-in, and for an
   admin's own per-user sign-in when no admin sign-in is left (My Tools
   then shows the upstream as unavailable, with no sign-in button). My
   Tools otherwise. The link is plain: the page itself checks who is
   signed in. (It used to carry a signed token that nothing ever read.)

Everything that sends goes through one ``SignInWarner``, built once at
startup and only while ``upstream_health_email_enabled`` is on. Callers
take ``SignInWarner | None`` and stay silent on None. The
``EmailSender`` port is ``SmtpEmailSender`` in prod and the logging
``StubEmailSender`` when no SMTP host is configured.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import structlog

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.domain.model.email_address import same_email
from mcpolis.domain.model.oauth_errors import TERMINAL_AUTH_ERROR_CODES
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import ADMIN_USER_ID
from mcpolis.domain.ports.email_sender import EmailSender
from mcpolis.domain.services.background_tasks import BackgroundTaskSet


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

HEALTH_EMAIL_INTERVAL = 60 * 60  # 1 hour

# Upper bound for one deletion's warning, all recipients included. The
# warning runs in the background, so this only stops a mail server that
# never answers from keeping the task alive forever.
WARNING_TIMEOUT_SECONDS = 120


class OrgFacts(Protocol):
    """What the warner needs to know about an org, read when it sends.
    ``app.py`` implements it over the runtime manager."""

    async def admin_emails(self, org_id: str) -> list[str]:
        """Every org admin's email (any role flagged ``is_admin``)."""
        ...

    def slug(self, org_id: str) -> str | None:
        """The org's slug, or None when unknown in this process."""
        ...

    def display_name(self, org_id: str) -> str | None:
        """The org's display name, or None when unknown."""
        ...

    async def has_admin_sign_in(
        self, org_id: str, upstream: UpstreamDefinition,
    ) -> bool:
        """Whether some admin still has a stored sign-in for an OAuth
        ``upstream`` (the admin sign-in the admin tab shows), whether or
        not an admin stopped the upstream."""
        ...

    async def is_member(self, org_id: str, email: str) -> bool:
        """Whether ``email`` is (still) a member of the org. A removed
        member or a pending invitation is not."""
        ...

    async def is_stopped(self, org_id: str, upstream_id: str) -> bool:
        """Whether an admin stopped the upstream: nobody can use it, so
        nobody needs to sign in to it again until it is started."""
        ...


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
    """Pure policy for the hourly sweep: when does a refresh-failure
    state warrant an email?

    - No recorded signature → nothing known-broken → no notify.
    - ``error_code`` not in ``TERMINAL_AUTH_ERROR_CODES`` → transient
      or unknown root cause; §5.1 will retry. Notifying here would
      spam users over upstream 5xx bursts. The terminal list is the
      one that decides deletion, so every sign-in deleted for a
      rejection gets its warning.
    - Already notified → stay quiet until ``clear_notified`` runs
      (i.e. a successful refresh has cleared the state).
    - Otherwise → notify.
    """
    if signature is None:
        return NotificationDecision(False, "no signature recorded")
    error_code = signature.get("error_code")
    if error_code not in TERMINAL_AUTH_ERROR_CODES:
        return NotificationDecision(
            False, f"error_code={error_code!r} is not terminal",
        )
    if already_notified:
        return NotificationDecision(False, "already notified")
    return NotificationDecision(True, str(error_code))


def build_reauth_link(
    *,
    server_url: str,
    org_slug: str | None,
    upstream_id: str,
    admin_page: bool,
) -> str:
    """A plain link to the page where the recipient signs in again:
    the MCP server's admin page when ``admin_page``, else My Tools.

    Without a known slug: bare ``/my-tools`` resolves to the viewer's own
    org, and ``/app`` routes an admin to their admin pages.
    """
    base = server_url.rstrip("/")
    if org_slug is None:
        return f"{base}/app" if admin_page else f"{base}/my-tools"
    org_path = f"{base}/orgs/{quote(org_slug, safe='')}"
    if admin_page:
        return f"{org_path}/admin/upstream/{quote(upstream_id, safe='')}"
    return f"{org_path}/my-tools"


def _render_email(
    *,
    upstream: UpstreamDefinition,
    org_name: str | None,
    reauth_link: str,
) -> tuple[str, str]:
    """Simple inline body — no template engine for the first cut.
    Returns (subject, body_text). HTML variant left for a follow-up
    once ops decides on branding / footer. Worded so it is true both
    after a deletion and for a sign-in the sweep found still stored,
    and names the org for people in several orgs."""
    subject = (
        f"Reconnect your {upstream.display_name} integration on MCP Hero"
    )
    where = f" in the {org_name} organization" if org_name else ""
    body = (
        f"The saved sign-in to {upstream.display_name}{where} on MCP Hero "
        f"has stopped working.\n"
        f"\n"
        f"Open the link below and sign in again to restore access:\n"
        f"{reauth_link}\n"
        f"\n"
        f"— MCP Hero (published by Nitsan Seniak)\n"
    )
    return subject, body


class SignInWarner:
    """Emails the people who must sign in to an upstream again.

    One object for every sender (the hourly sweep, and both places that
    delete a sign-in the upstream refused), so they cannot drift in who
    they reach or where the link points.
    """

    def __init__(
        self,
        *,
        email_sender: EmailSender,
        orgs: OrgFacts,
        server_url: str,
        timeout_seconds: float = WARNING_TIMEOUT_SECONDS,
    ) -> None:
        self.email_sender = email_sender
        self.orgs = orgs
        self.server_url = server_url
        self._timeout_seconds = timeout_seconds
        # Holds each warning until it ends: the event loop keeps only
        # weak references, so a dropped task could be collected mid-send.
        # The shutdown waits for them too (``drain_every_set``).
        self._background_tasks = BackgroundTaskSet()

    async def send(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
    ) -> int:
        """Email everyone who can redo ``user_id``'s sign-in to
        ``upstream``. Returns how many emails went out.

        - ``admin_oauth`` → every org admin. Admin sign-ins are stored
          under each admin's own email, so every admin hears about any
          one of them.
        - ``per_user_oauth`` → ``user_id`` itself, while it is a member
          of the org: a removed member is not told to sign in again to
          an org they left.
        - anything else has no sign-in to redo → nobody.

        Nobody is emailed about an upstream an admin stopped: nobody can
        use it, and Start is what brings it back.

        One recipient's send failure is logged and skipped, so the others
        still get theirs.
        """
        if await self.orgs.is_stopped(org_id, upstream.id):
            logger.info(
                "upstream.health.notify.skipped.stopped",
                org_id=org_id,
                upstream_id=upstream.id,
                user=user_id,
            )
            return 0
        if upstream.auth.mode == AuthMode.admin_oauth:
            recipients = await self.orgs.admin_emails(org_id)
        elif upstream.auth.mode == AuthMode.per_user_oauth:
            if not await self.orgs.is_member(org_id, user_id):
                logger.info(
                    "upstream.health.notify.skipped.not_a_member",
                    org_id=org_id,
                    upstream_id=upstream.id,
                    user=user_id,
                )
                return 0
            recipients = [user_id]
        else:
            logger.debug(
                "upstream.health.notify.skipped.no_reauth_flow",
                org_id=org_id,
                upstream_id=upstream.id,
                user=user_id,
                auth_mode=upstream.auth.mode.value,
            )
            return 0
        if not recipients:
            logger.warning(
                "upstream.health.notify.no_recipients",
                org_id=org_id,
                upstream_id=upstream.id,
                user=user_id,
            )
            return 0

        admin_page = await self._needs_admin_page(org_id, upstream, user_id)
        link = build_reauth_link(
            server_url=self.server_url,
            org_slug=self.orgs.slug(org_id),
            upstream_id=upstream.id,
            admin_page=admin_page,
        )
        subject, body = _render_email(
            upstream=upstream,
            org_name=self.orgs.display_name(org_id),
            reauth_link=link,
        )
        sent = 0
        for recipient in recipients:
            try:
                await self.email_sender.send_email(
                    to=recipient, subject=subject, body_text=body,
                )
                sent += 1
            except Exception:
                logger.exception(
                    "upstream.health.notify.failed",
                    org_id=org_id,
                    upstream_id=upstream.id,
                    user=user_id,
                    recipient=recipient,
                )
        return sent

    async def _needs_admin_page(
        self,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
    ) -> bool:
        """Whether the link must open the MCP server's admin page rather
        than My Tools. My Tools has no sign-in button for an admin
        sign-in, nor for any OAuth upstream while no admin sign-in is
        left (it shows the upstream as unavailable). The admin page has
        one in both cases; only admins can open it."""
        if upstream.auth.mode == AuthMode.admin_oauth:
            return True
        if await self.orgs.has_admin_sign_in(org_id, upstream):
            return False
        return any(
            same_email(user_id, admin)
            for admin in await self.orgs.admin_emails(org_id)
        )

    def warn_deleted(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
    ) -> None:
        """The gateway just deleted ``user_id``'s sign-in because the
        upstream refused it: tell whoever must sign in again.

        Returns at once; the email goes out in a background task. The
        caller is a reconnect that other requests may be waiting on
        (gateway tool calls, a dashboard Start), so it must not wait for
        the mail server, and a caller giving up must not cancel the
        email: the sign-in is already gone, and nothing would retry.

        Called only once the delete succeeded (``delete_refused_sign_in``),
        so a sign-in replaced meanwhile never triggers it, and two
        reconnects racing over one sign-in email once (only one delete
        can succeed). It leaves the "already notified" marker alone: that
        marker belongs to the sweep, which only looks at stored sign-ins.
        """
        self._background_tasks.spawn(
            self._warn_deleted(org_id=org_id, upstream=upstream, user_id=user_id),
            name=f"sign-in-warning:{org_id}:{upstream.id}",
        )

    async def _warn_deleted(
        self,
        *,
        org_id: str,
        upstream: UpstreamDefinition,
        user_id: str,
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                if (
                    upstream.auth.mode == AuthMode.admin_oauth
                    and await self.orgs.has_admin_sign_in(org_id, upstream)
                ):
                    # Another admin's sign-in still serves the org (or the
                    # deleted row was a stray one): nobody needs to act.
                    logger.info(
                        "upstream.health.sign_in_deleted.still_signed_in",
                        org_id=org_id,
                        upstream_id=upstream.id,
                        user=user_id,
                    )
                    return
                sent = await self.send(
                    org_id=org_id, upstream=upstream, user_id=user_id,
                )
        except Exception:
            # TimeoutError included: the mail server never answered.
            logger.exception(
                "upstream.health.sign_in_deleted.warn_failed",
                org_id=org_id,
                upstream_id=upstream.id,
                user=user_id,
            )
            return
        logger.info(
            "upstream.health.sign_in_deleted.warned",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
            emails_sent=sent,
        )

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for the warnings still being sent, at most ``timeout``
        seconds (None = no limit). The shutdown waits for them with every
        other background job (``drain_every_set``); tests call this."""
        await self._background_tasks.drain(timeout)


async def check_and_notify_upstream(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    connection_store: ConnectionStore,
    warner: SignInWarner,
) -> bool:
    """Sweep one stored ``(upstream, user)`` pair. Send email(s) if the
    recorded signature is terminal AND we haven't already notified.
    Returns True iff at least one email was sent.

    Mark-as-notified is keyed per ``(upstream, user_id)``. Nothing sent
    (no admins, every send failed) → not marked, so fixing the cause
    lets the next tick deliver it.
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

    sent = await warner.send(org_id=org_id, upstream=upstream, user_id=user_id)
    if sent:
        await connection_store.mark_notified(
            org_id, upstream.id, user_id,
        )
        logger.info(
            "upstream.health.notify.sent",
            org_id=org_id,
            upstream_id=upstream.id,
            user=user_id,
            recipient_count=sent,
        )
    return sent > 0


async def run_health_check_for_org(
    *,
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    warner: SignInWarner,
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
        # Pre-existing gap: admin sign-ins are now stored under each
        # admin's email, never ADMIN_USER_ID, so this skips all of them
        # and the sweep never covers admin_oauth. Deletions still warn
        # inline (``SignInWarner.warn_deleted``).
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
                warner=warner,
            )
        except Exception:
            logger.exception(
                "upstream.health.iteration.failed",
                org_id=org_id,
                upstream_id=upstream_id,
                user=user_id,
            )
