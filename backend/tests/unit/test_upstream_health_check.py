"""Tests for §5.2 — proactive health check + email notification.

Motivation (``internal/documents/oauth-durability.md`` §5.2): when a user's
upstream refresh genuinely dies (§3.4 revocation), they first learn
about it mid-task in Claude. §5.2 sends an email before that
happens.

The module under test (``upstream_health_check.py``) splits cleanly
into three testable layers:

1. ``decide_notification`` — pure policy function: given a signature
   and "already-notified" bool, should we email? Branch-by-branch
   table-test.

2. ``build_reauth_link`` — a plain link to the page where the
   recipient signs in again (My Tools, or the MCP server's admin page
   for an admin sign-in).

3. ``check_and_notify_upstream`` — the orchestration: reads
   per-user signature from the connection store, resolves recipients
   (admin list via callback for admin_oauth; ``user_id`` itself for
   per_user_oauth) through ``SignInWarner``, calls the ``EmailSender``
   stub, marks as
   notified. Driven with a real ``FileConnectionStore`` and
   ``StubEmailSender`` so the full storage → decision →
   email-record loop runs end-to-end without mocks standing in for
   each layer.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from mcpolis.adapters.email.stub_email_sender import StubEmailSender
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import ADMIN_USER_ID, DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_health_check import (
    SignInWarner,
    build_reauth_link,
    check_and_notify_upstream,
    decide_notification,
)
from tests.unit.factories import (
    FakeOrgFacts,
    make_oauth_upstream,
    make_sign_in_warner,
)


UPSTREAM_ID = "notion"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"


def _make_upstream(
    mode: AuthMode = AuthMode.admin_oauth,
    *,
    upstream_id: str = UPSTREAM_ID,
    display_name: str = "Notion",
) -> UpstreamDefinition:
    return make_oauth_upstream(
        id=upstream_id, display_name=display_name,
        mode=mode, url=UPSTREAM_URL,
    )


def _invalid_grant_signature() -> dict[str, object]:
    return {
        "status_code": 400,
        "body_excerpt": '{"error":"invalid_grant"}',
        "error_code": "invalid_grant",
        "timestamp": "2026-04-24T12:00:00+00:00",
    }


# ── decide_notification unit tests ───────────────────────────────────


def test_decide_no_signature_does_not_notify() -> None:
    """No recorded signature → we have no proof anything is broken.
    Notifying here would be a false positive that trains users to
    ignore the emails."""
    decision = decide_notification(signature=None, already_notified=False)
    assert decision.should_notify is False


def test_decide_transient_error_code_does_not_notify() -> None:
    """A 5xx or unknown body → transient. §5.1 is still retrying;
    emailing the user about "please re-auth" while the upstream is
    just having a bad minute would be wrong."""
    for code in (None, "server_error", "temporarily_unavailable"):
        sig = {**_invalid_grant_signature(), "error_code": code}
        decision = decide_notification(signature=sig, already_notified=False)
        assert decision.should_notify is False, (
            f"error_code={code!r} should not trigger notification"
        )


def test_decide_invalid_grant_notifies_once() -> None:
    """The canonical positive case: invalid_grant + not yet
    notified → send. The next tick, already_notified flips True and
    we stop. Re-notification only resumes after ``clear_notified``
    (invoked from the success paths)."""
    sig = _invalid_grant_signature()
    first = decide_notification(signature=sig, already_notified=False)
    assert first.should_notify is True

    second = decide_notification(signature=sig, already_notified=True)
    assert second.should_notify is False


def test_decide_invalid_client_notifies_once() -> None:
    """``invalid_client`` (the app registration is dead) deletes the
    sign-in at once, exactly like ``invalid_grant``. The member is
    signed out either way, so either way they get the warning email."""
    sig = {**_invalid_grant_signature(), "error_code": "invalid_client"}
    first = decide_notification(signature=sig, already_notified=False)
    assert first.should_notify is True

    second = decide_notification(signature=sig, already_notified=True)
    assert second.should_notify is False


# ── build_reauth_link ────────────────────────────────────────────────


def test_reauth_link_opens_my_tools_or_the_mcp_server_admin_page() -> None:
    """Plain links to the page with the sign-in button: My Tools, or the
    MCP server's admin page. The page checks who is signed in."""
    my_tools = build_reauth_link(
        server_url=SERVER_URL + "/",
        org_slug="acme",
        upstream_id=UPSTREAM_ID,
        admin_page=False,
    )
    admin_page = build_reauth_link(
        server_url=SERVER_URL,
        org_slug="acme",
        upstream_id=UPSTREAM_ID,
        admin_page=True,
    )
    assert my_tools == f"{SERVER_URL}/orgs/acme/my-tools"
    assert admin_page == f"{SERVER_URL}/orgs/acme/admin/upstream/{UPSTREAM_ID}"


def test_reauth_link_without_a_known_org_falls_back_to_routing_pages() -> None:
    """Unknown slug: bare ``/my-tools`` resolves the viewer's own org,
    and ``/app`` sends an admin to their admin pages."""
    my_tools = build_reauth_link(
        server_url=SERVER_URL,
        org_slug=None,
        upstream_id=UPSTREAM_ID,
        admin_page=False,
    )
    admin_page = build_reauth_link(
        server_url=SERVER_URL,
        org_slug=None,
        upstream_id=UPSTREAM_ID,
        admin_page=True,
    )
    assert my_tools == f"{SERVER_URL}/my-tools"
    assert admin_page == f"{SERVER_URL}/app"


# ── check_and_notify_upstream — admin_oauth ──────────────────────────


async def _seed_invalid_grant_signature(
    store: FileConnectionStore,
    *,
    user_id: str,
    upstream_id: str = UPSTREAM_ID,
) -> None:
    await store.record_refresh_failure(
        DEFAULT_ORG_ID, upstream_id, user_id,
        signature=_invalid_grant_signature(),
    )


ADMINS = ["admin1@co.com", "admin2@co.com"]


@pytest.mark.asyncio
async def test_admin_oauth_notifies_every_org_admin_once(
    tmp_path: Path,
) -> None:
    """admin_oauth + invalid_grant → each admin in the org gets one
    email. The notified flag is keyed per ``(upstream, ADMIN_USER_ID)``
    so the next hourly tick doesn't re-send."""
    store = FileConnectionStore(tmp_path)
    await _seed_invalid_grant_signature(store, user_id=ADMIN_USER_ID)
    sender = StubEmailSender()

    sent = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id=ADMIN_USER_ID,
        connection_store=store,
        warner=make_sign_in_warner(sender, ADMINS),
    )
    assert sent is True
    assert len(sender.sent) == 2
    recipients = {m.to for m in sender.sent}
    assert recipients == {"admin1@co.com", "admin2@co.com"}
    assert all("Notion" in m.subject for m in sender.sent)
    assert all(
        f"{SERVER_URL}/orgs/acme/admin/upstream/{UPSTREAM_ID}" in m.body_text
        for m in sender.sent
    )

    # Marked as notified → next call is a no-op.
    sent_again = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id=ADMIN_USER_ID,
        connection_store=store,
        warner=make_sign_in_warner(sender, ADMINS),
    )
    assert sent_again is False
    assert len(sender.sent) == 2


@pytest.mark.asyncio
async def test_admin_oauth_no_admins_logged_but_not_marked(
    tmp_path: Path,
) -> None:
    """If the resolver returns no admins (mis-config, org with no
    admin role yet), DO NOT mark as notified — otherwise fixing the
    config wouldn't retroactively deliver the email."""
    store = FileConnectionStore(tmp_path)
    await _seed_invalid_grant_signature(store, user_id=ADMIN_USER_ID)
    sender = StubEmailSender()

    sent = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id=ADMIN_USER_ID,
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert sent is False
    assert sender.sent == []
    assert await store.was_notified(
        DEFAULT_ORG_ID, UPSTREAM_ID, ADMIN_USER_ID,
    ) is False


# ── check_and_notify_upstream — per_user_oauth ───────────────────────


@pytest.mark.asyncio
async def test_per_user_oauth_notifies_only_that_user(
    tmp_path: Path,
) -> None:
    """per_user_oauth + invalid_grant for alice → alice (and only
    alice) gets an email. bob's absence of a signature means his
    next run of the loop skips him entirely."""
    store = FileConnectionStore(tmp_path)
    await _seed_invalid_grant_signature(store, user_id="alice@co.com")
    sender = StubEmailSender()

    sent = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert sent is True
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "alice@co.com"
    assert f"{SERVER_URL}/orgs/acme/my-tools" in sender.sent[0].body_text

    # Bob has no signature → decide_notification short-circuits.
    sent_bob = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="bob@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert sent_bob is False
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_transient_signature_does_not_notify(
    tmp_path: Path,
) -> None:
    """A stored signature with ``error_code != 'invalid_grant'`` is
    §5.1's transient case. Leave the user alone; §5.1 will retry."""
    store = FileConnectionStore(tmp_path)
    await store.record_refresh_failure(
        DEFAULT_ORG_ID, UPSTREAM_ID, "alice@co.com",
        signature={
            "status_code": 502,
            "body_excerpt": "<html>bad gateway</html>",
            "error_code": None,
            "timestamp": "2026-04-24T12:00:00+00:00",
        },
    )
    sender = StubEmailSender()

    sent = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert sent is False
    assert sender.sent == []


@pytest.mark.asyncio
async def test_success_path_clears_notified_flag(
    tmp_path: Path,
) -> None:
    """After a successful reconnect, ``clear_notified`` runs and the
    next invalid_grant failure must trigger a fresh email. Pins the
    ``mark_notified`` / ``clear_notified`` pairing that keeps users
    from getting one-and-done'd when a real repeated failure lands
    months later."""
    store = FileConnectionStore(tmp_path)
    await _seed_invalid_grant_signature(store, user_id="alice@co.com")
    sender = StubEmailSender()

    await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert len(sender.sent) == 1

    # Simulate a successful reconnect clearing state.
    await store.reset_refresh_failures(
        DEFAULT_ORG_ID, UPSTREAM_ID, "alice@co.com",
    )
    await store.clear_notified(
        DEFAULT_ORG_ID, UPSTREAM_ID, "alice@co.com",
    )

    # New failure later → emails again.
    await _seed_invalid_grant_signature(store, user_id="alice@co.com")
    await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender),
    )
    assert len(sender.sent) == 2


@pytest.mark.asyncio
async def test_service_account_upstream_never_notifies(
    tmp_path: Path,
) -> None:
    """Service-account upstreams don't have a user OAuth flow to
    re-enter. Even if their failure row somehow contained
    invalid_grant, no email is appropriate — there's no user to
    redirect."""
    store = FileConnectionStore(tmp_path)
    await _seed_invalid_grant_signature(store, user_id="alice@co.com")
    sender = StubEmailSender()

    sent = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.service_account),
        user_id="alice@co.com",
        connection_store=store,
        warner=make_sign_in_warner(sender, ADMINS),
    )
    assert sent is False
    assert sender.sent == []



# ── SignInWarner.warn_deleted — after a deletion ─────────────────────


@pytest.mark.asyncio
async def test_deleted_per_user_sign_in_warns_the_member_with_a_my_tools_link() -> None:
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, ADMINS, admin_signed_in=True)

    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
    )
    await warner.drain()

    assert [m.to for m in sender.sent] == ["alice@co.com"]
    assert f"{SERVER_URL}/orgs/acme/my-tools" in sender.sent[0].body_text


@pytest.mark.asyncio
async def test_deleted_last_admin_per_user_sign_in_links_the_mcp_server_admin_page() -> None:
    """A per-user upstream is usable only while some admin has a stored
    sign-in. When the deleted one was the last, My Tools shows the
    upstream as unavailable with no sign-in button, so the admin is sent
    to the MCP server's admin page, which has one."""
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, ["owner@co.com"], admin_signed_in=False)

    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="owner@co.com",
    )
    await warner.drain()

    assert [m.to for m in sender.sent] == ["owner@co.com"]
    assert (
        f"{SERVER_URL}/orgs/acme/admin/upstream/{UPSTREAM_ID}"
        in sender.sent[0].body_text
    )


@pytest.mark.asyncio
async def test_deleted_admin_sign_in_warns_nobody_while_another_admin_sign_in_serves() -> None:
    """An admin sign-in deleted while another admin's still serves the
    org (a stale second admin row, or a stray member row left by a mode
    switch): the org's shared sign-in works, nobody needs to act."""
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, ADMINS, admin_signed_in=True)

    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id="admin2@co.com",
    )
    await warner.drain()

    assert sender.sent == []


@pytest.mark.asyncio
async def test_deleted_last_admin_sign_in_warns_every_admin() -> None:
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, ADMINS, admin_signed_in=False)

    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id="admin1@co.com",
    )
    await warner.drain()

    assert sorted(m.to for m in sender.sent) == ADMINS
    assert all(
        f"{SERVER_URL}/orgs/acme/admin/upstream/{UPSTREAM_ID}" in m.body_text
        for m in sender.sent
    )


@pytest.mark.asyncio
async def test_warning_email_names_the_org_and_claims_nothing_false() -> None:
    """The email goes out after the sign-in is gone, so it must not say
    "safe to ignore, nothing changes"; and it names the org, for people
    in several orgs."""
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, org_name="Acme Corp")

    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="alice@co.com",
    )
    await warner.drain()

    body = sender.sent[0].body_text
    assert "Notion in the Acme Corp organization" in body
    assert "safe to ignore" not in body
    assert "Nothing changes" not in body


class NeverAnsweringSender:
    """A mail server that accepts the connection and never answers."""

    async def send_email(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        del to, subject, body_text, body_html
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_warning_to_a_silent_mail_server_ends_at_the_time_limit() -> None:
    """The warning runs in the background; its time limit stops a mail
    server that never answers from keeping it alive forever."""
    warner = SignInWarner(
        email_sender=NeverAnsweringSender(),
        orgs=FakeOrgFacts(),
        server_url=SERVER_URL,
        timeout_seconds=0.1,
    )

    with structlog.testing.capture_logs() as logs:
        warner.warn_deleted(
            org_id=DEFAULT_ORG_ID,
            upstream=_make_upstream(AuthMode.per_user_oauth),
            user_id="alice@co.com",
        )
        await asyncio.wait_for(warner.drain(), timeout=5)

    assert [
        e for e in logs
        if e.get("event") == "upstream.health.sign_in_deleted.warn_failed"
    ], f"expected warn_failed after the time limit, got: {logs}"
