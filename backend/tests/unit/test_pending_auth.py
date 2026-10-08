"""Tests for PendingAuthCoordinator with signed state tokens."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from mcpolis.adapters.auth.hmac_token import verify_token
from mcpolis.adapters.auth.pending_auth import (
    STATE_TOKEN_MAX_AGE,
    PendingAuth,
    PendingAuthCoordinator,
)
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from tests.unit.test_dashboard_api import make_test_client

DEV = "dev@example.com"  # a member in ``make_test_client``


def make_signing_key() -> bytes:
    return hashlib.sha256(b"test-secret").digest()


async def make_flow_with_link(
    coordinator: PendingAuthCoordinator,
    org_id: str,
    upstream_id: str,
    user_id: str,
    state: str,
) -> PendingAuth:
    """A flow whose sign-in link went out, with the upstream's ``state``:
    the callback for it carries that state."""
    pending = coordinator.create_pending(org_id, upstream_id, user_id)
    await pending.redirect_handler(f"https://idp.example/authorize?state={state}")
    return pending


def signed_state_of(pending: PendingAuth) -> str:
    """The signed ``state`` the upstream sends back to the callback, as
    the gateway put it in the flow's sign-in link."""
    assert pending.redirect_url is not None
    return parse_qs(urlparse(pending.redirect_url).query)["state"][0]


@pytest.mark.asyncio
async def test_redirect_handler_replaces_state_with_signed_token() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    await pending.redirect_handler(
        "https://github.com/login/oauth/authorize?state=abc123&scope=repo"
    )

    assert pending.redirect_url is not None
    assert pending.auth_state == "abc123"

    # The URL should contain a signed state, not the original
    parsed = urlparse(pending.redirect_url)
    params = parse_qs(parsed.query)
    signed_state = params["state"][0]

    # Verify the signed token
    payload = verify_token(signed_state, key, max_age=STATE_TOKEN_MAX_AGE)
    assert payload is not None
    assert payload["org"] == "acme"
    assert payload["uid"] == "github"
    assert payload["usr"] == "alice"
    assert payload["ost"] == "abc123"

    # scope should still be there
    assert params["scope"] == ["repo"]


@pytest.mark.asyncio
async def test_complete_by_key_signals_callback_handler() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    await pending.redirect_handler(
        "https://example.com/auth?state=xyz"
    )

    async def complete_later() -> None:
        await asyncio.sleep(0.01)
        coordinator.complete_by_key(
            "acme", "github", "alice", "the-code", "xyz"
        )

    asyncio.create_task(complete_later())

    code, state = await pending.callback_handler()
    assert code == "the-code"
    assert state == "xyz"


@pytest.mark.asyncio
async def test_wait_for_redirect_url() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "slack", "bob")

    async def redirect_later() -> None:
        await asyncio.sleep(0.01)
        await pending.redirect_handler(
            "https://slack.com/oauth?state=s1"
        )

    asyncio.create_task(redirect_later())

    url = await pending.wait_for_redirect_or_refresh()
    assert url is not None
    assert "slack.com/oauth" in url


@pytest.mark.asyncio
async def test_complete_by_key_unknown_returns_none() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    result = coordinator.complete_by_key(
        "acme", "unknown", "user", "code", "state"
    )
    assert result is None


@pytest.mark.asyncio
async def test_create_pending_replaces_existing() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)

    pending1 = await make_flow_with_link(coordinator, "acme", "github", "alice", "st")
    pending2 = await make_flow_with_link(coordinator, "acme", "github", "alice", "st")
    assert pending2 is not pending1

    # Old pending should be gone
    result = coordinator.complete_by_key(
        "acme", "github", "alice", "code", "st"
    )
    assert result is pending2


@pytest.mark.asyncio
async def test_cleanup_removes_pending() -> None:
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    coordinator.create_pending("acme", "github", "alice")

    coordinator.cleanup("acme", "github", "alice")

    assert coordinator.get_pending("acme", "github", "alice") is None
    assert coordinator.complete_by_key(
        "acme", "github", "alice", "code", "st"
    ) is None


@pytest.mark.asyncio
async def test_pre_filled_code_completes_callback_immediately() -> None:
    """Simulates the restart recovery path: code is pre-filled before
    the background task starts, so callback_handler returns instantly."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "github", "alice")

    # Pre-fill the code (as initiate_oauth_connection does after restart)
    pending.complete("stored-code", "original-state")

    # callback_handler should return immediately
    code, state = await asyncio.wait_for(
        pending.callback_handler(), timeout=1.0
    )
    assert code == "stored-code"
    assert state == "original-state"


@pytest.mark.asyncio
async def test_same_user_same_upstream_different_orgs_do_not_collide() -> None:
    """Superadmin who kicks off OAuth for the same upstream in two orgs
    in parallel must see both PendingAuths preserved, keyed by org."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)

    pending_a = await make_flow_with_link(
        coordinator, "acme", "github", "__admin__", "st-a",
    )
    pending_b = await make_flow_with_link(
        coordinator, "beta", "github", "__admin__", "st-b",
    )
    assert pending_a is not pending_b

    # Completing one does not consume the other
    result_a = coordinator.complete_by_key(
        "acme", "github", "__admin__", "code-a", "st-a"
    )
    assert result_a is pending_a

    # Beta's pending is still there
    assert coordinator.get_pending(
        "beta", "github", "__admin__"
    ) is pending_b

    result_b = coordinator.complete_by_key(
        "beta", "github", "__admin__", "code-b", "st-b"
    )
    assert result_b is pending_b


@pytest.mark.asyncio
async def test_signed_state_token_carries_org_for_callback() -> None:
    """Callback route reads the org from the signed state token, not
    from the session cookie. Make sure the token actually carries it."""
    key = make_signing_key()
    coordinator = PendingAuthCoordinator(key)
    pending = coordinator.create_pending("acme", "mixpanel", "__admin__")

    await pending.redirect_handler(
        "https://mixpanel.com/oauth/authorize?state=orig"
    )

    assert pending.redirect_url is not None
    signed_state = parse_qs(urlparse(pending.redirect_url).query)["state"][0]
    payload = verify_token(signed_state, key, max_age=STATE_TOKEN_MAX_AGE)
    assert payload is not None
    assert payload["org"] == "acme"


# --- A callback reaches only the flow that waits for it ---


def test_a_flow_waits_for_its_callback_as_long_as_its_link_is_accepted() -> None:
    """The wait used to end after 5 minutes while the sign-in link stayed
    valid for 10: a person who took 6 minutes on the consent page got
    "Authorization successful" and no sign-in."""
    coordinator = PendingAuthCoordinator(make_signing_key())

    pending = coordinator.create_pending("acme", "mixpanel", "dev")

    assert pending.auth_timeout == STATE_TOKEN_MAX_AGE


async def test_a_callback_after_the_flow_stopped_waiting_is_not_consumed() -> None:
    coordinator = PendingAuthCoordinator(make_signing_key(), auth_timeout=0.05)
    pending = await make_flow_with_link(coordinator, "acme", "mixpanel", "dev", "s1")

    # The sign-in library's wait for the code gives up.
    with pytest.raises(TimeoutError):
        await pending.callback_handler()

    consumed = coordinator.complete_by_key("acme", "mixpanel", "dev", "late-code", "s1")

    assert consumed is None, (
        "the late code went to a flow nobody waits for: the callback said "
        "success and the code was lost"
    )


async def test_a_stale_callback_does_not_complete_a_newer_sign_in() -> None:
    """A second Connect click starts a newer flow; the first consent
    page's callback (its own state) must not hand the newer flow its
    code: the sign-in library would refuse it on the state, failing the
    newer sign-in too."""
    coordinator = PendingAuthCoordinator(make_signing_key())
    await make_flow_with_link(coordinator, "acme", "mixpanel", "dev", "s1")
    newer = await make_flow_with_link(coordinator, "acme", "mixpanel", "dev", "s2")

    consumed = coordinator.complete_by_key("acme", "mixpanel", "dev", "code-1", "s1")

    assert consumed is None
    assert newer.auth_code is None
    assert coordinator.complete_by_key(
        "acme", "mixpanel", "dev", "code-2", "s2",
    ) is newer
    assert newer.auth_code == "code-2"


def test_the_callback_of_a_replaced_sign_in_says_so_and_leaves_the_newer_one(
    tmp_path: Path,
) -> None:
    """The person clicked Connect twice and finishes the first consent
    page: the callback tells them to use the newest sign-in window, and
    keeps the older code out of both the newer sign-in and the store (a
    later Connect would pick it up and fail with it)."""
    client = make_test_client(tmp_path)
    coordinator: PendingAuthCoordinator = client.app.state.auth_coordinator  # type: ignore[attr-defined]
    older = asyncio.run(make_flow_with_link(coordinator, "default", "mixpanel", DEV, "s1"))
    newer = asyncio.run(make_flow_with_link(coordinator, "default", "mixpanel", DEV, "s2"))

    resp = client.get(
        "/api/oauth/upstream/callback",
        params={"code": "older-code", "state": signed_state_of(older)},
    )

    assert "replaced by a newer one" in resp.text
    assert newer.auth_code is None
    assert coordinator.get_pending("default", "mixpanel", DEV) is newer
    store = FileConnectionStore(tmp_path / "data")
    assert asyncio.run(store.pop_pending_code("default", "mixpanel", DEV)) is None


async def test_removal_from_one_org_leaves_the_other_orgs_flows() -> None:
    coordinator = PendingAuthCoordinator(make_signing_key())
    here = await make_flow_with_link(coordinator, "acme", "mixpanel", "dev", "s1")
    there = await make_flow_with_link(coordinator, "globex", "mixpanel", "dev", "s2")

    assert coordinator.abort_for_user("acme", "dev") == 1

    assert here.aborted and not there.aborted
    assert coordinator.complete_by_key(
        "globex", "mixpanel", "dev", "code", "s2",
    ) is there
