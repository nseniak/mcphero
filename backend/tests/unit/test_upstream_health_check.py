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

2. ``build_reauth_link`` — HMAC-signed link using the existing
   OAuth-state token format (``sign_token`` + ``verify_token``).
   Tested by verifying with the same key decodes the payload.

3. ``check_and_notify_upstream`` — the orchestration: reads
   per-user signature from the connection store, resolves recipients
   (admin list via callback for admin_oauth; ``user_id`` itself for
   per_user_oauth), calls the ``EmailSender`` stub, marks as
   notified. Driven with a real ``FileConnectionStore`` and
   ``StubEmailSender`` so the full storage → decision →
   email-record loop runs end-to-end without mocks standing in for
   each layer.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcpolis.adapters.auth.hmac_token import verify_token
from mcpolis.adapters.email.stub_email_sender import StubEmailSender
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import ADMIN_USER_ID, DEFAULT_ORG_ID
from mcpolis.domain.services.upstream_health_check import (
    build_reauth_link,
    check_and_notify_upstream,
    decide_notification,
)
from tests.unit.factories import make_oauth_upstream


UPSTREAM_ID = "notion"
UPSTREAM_URL = "https://mcp.example.invalid/mcp"
SERVER_URL = "https://gateway.example.invalid"
HMAC_KEY = b"unit-test-key"


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


# ── build_reauth_link ────────────────────────────────────────────────


def test_reauth_link_is_hmac_verifiable() -> None:
    """The same HMAC key that signs the link must verify it back to
    the expected payload shape. Mirrors the OAuth-state-token flow so
    we're not inventing parallel crypto."""
    link = build_reauth_link(
        server_url=SERVER_URL,
        org_id="org-1",
        upstream_id=UPSTREAM_ID,
        user_id="alice@co.com",
        key=HMAC_KEY,
        now=time.time(),
    )
    assert link.startswith(f"{SERVER_URL}/my-tools?reauth=")
    token = link.split("?reauth=", 1)[1]
    payload = verify_token(token, HMAC_KEY)
    assert payload is not None
    assert payload["kind"] == "upstream_reauth"
    assert payload["org_id"] == "org-1"
    assert payload["upstream_id"] == UPSTREAM_ID
    assert payload["user_id"] == "alice@co.com"


def test_reauth_link_rejects_wrong_key() -> None:
    """A link signed with key A must not verify under key B. Stops
    a leaked HMAC key from one environment from creating valid
    re-auth links for another."""
    link = build_reauth_link(
        server_url=SERVER_URL,
        org_id="org-1",
        upstream_id=UPSTREAM_ID,
        user_id="alice@co.com",
        key=HMAC_KEY,
    )
    token = link.split("?reauth=", 1)[1]
    assert verify_token(token, b"different-key") is None


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


async def _admin_resolver(_org_id: str) -> list[str]:
    return ["admin1@co.com", "admin2@co.com"]


async def _empty_resolver(_org_id: str) -> list[str]:
    return []


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
        email_sender=sender,
        admin_email_resolver=_admin_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
    )
    assert sent is True
    assert len(sender.sent) == 2
    recipients = {m.to for m in sender.sent}
    assert recipients == {"admin1@co.com", "admin2@co.com"}
    assert all("Notion" in m.subject for m in sender.sent)
    assert all(
        "my-tools?reauth=" in m.body_text for m in sender.sent
    )

    # Marked as notified → next call is a no-op.
    sent_again = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.admin_oauth),
        user_id=ADMIN_USER_ID,
        connection_store=store,
        email_sender=sender,
        admin_email_resolver=_admin_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
    )
    assert sent is True
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "alice@co.com"

    # Bob has no signature → decide_notification short-circuits.
    sent_bob = await check_and_notify_upstream(
        org_id=DEFAULT_ORG_ID,
        upstream=_make_upstream(AuthMode.per_user_oauth),
        user_id="bob@co.com",
        connection_store=store,
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_empty_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
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
        email_sender=sender,
        admin_email_resolver=_admin_resolver,
        server_url=SERVER_URL,
        hmac_key=HMAC_KEY,
    )
    assert sent is False
    assert sender.sent == []
