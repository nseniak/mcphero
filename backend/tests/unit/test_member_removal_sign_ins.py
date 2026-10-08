"""Removing a member ends their upstream sign-ins, including one still in
progress, and nothing afterwards brings a sign-in back or emails them
about the org they left.

- Removal aborts the member's sign-in flows still in progress (they
  clicked Connect and are on the upstream's consent page): the flow can
  no longer be completed, and a code that already arrived saves nothing.
- The upstream's callback re-checks membership before it completes.
- The periodic token refresh skips sign-ins of non-members.
- The sign-in warner emails nobody about a non-member's sign-in, and
  nobody about an MCP an admin stopped.
- Flows nobody completes are forgotten after the sign-in link expires.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import structlog
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthToken as SdkToken

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.auth.pending_auth import (
    STATE_TOKEN_MAX_AGE,
    PendingAuth,
    PendingAuthCoordinator,
    SignInAborted,
)
from mcpolis.adapters.email.stub_email_sender import StubEmailSender
from mcpolis.adapters.repositories.connection_store import OAuthToken, SavedSignIn
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
# ``org_runtime`` first: ``oauth_refresh`` and ``upstream_connection_service``
# import each other, and only that order resolves.
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.oauth_refresh import refresh_org_sign_ins
from mcpolis.domain.services import upstream_connection_service as ucs
from mcpolis.domain.services.upstream_connection_service import (
    initiate_oauth_connection,
)
from mcpolis.entrypoints.app import (
    _refresh_sign_ins_of_org,  # pyright: ignore[reportPrivateUsage]
    _RuntimeOrgFacts,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit._fake_oauth_upstream import (
    FakeOAuthUpstream,
    make_oauth_protected_upstream,
    start_fake_oauth_upstream,
)
from tests.unit._user_session_harness import stop_upstream
from tests.unit.factories import make_oauth_upstream, make_sign_in_warner
from tests.unit.test_dashboard_api import make_test_client

DEV = "dev@example.com"  # a member in ``make_test_client``
ADMIN = "admin@example.com"


def coordinator_of(client: TestClient) -> PendingAuthCoordinator:
    return client.app.state.auth_coordinator  # type: ignore[attr-defined,no-any-return]


def make_live_token(access_token: str) -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        refresh_token=f"{access_token}-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )


async def signed_state_for(pending: PendingAuth) -> str:
    """The signed ``state`` the upstream will send back to the callback,
    as the gateway puts it in the sign-in link."""
    await pending.redirect_handler(
        "https://idp.example.invalid/authorize?state=upstream-state",
    )
    assert pending.redirect_url is not None
    return parse_qs(urlparse(pending.redirect_url).query)["state"][0]


# --- A sign-in in progress when the member is removed ---


def test_removal_aborts_the_members_sign_in_in_progress(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    pending = coordinator_of(client).create_pending("default", "mixpanel", DEV)

    removed = client.delete("/api/admin/users/dev%40example.com")
    assert removed.status_code == 200, removed.text

    assert pending.aborted
    completed = coordinator_of(client).complete_by_key(
        "default", "mixpanel", DEV, "code-from-upstream", "upstream-state",
    )
    assert completed is None


async def test_an_aborted_flow_exchanges_no_code_and_saves_no_sign_in(
    tmp_path: Path,
) -> None:
    """The code already arrived (the flow left the coordinator's table)
    when the removal aborts it: the SDK's wait for the code raises, and a
    fresh sign-in written anyway is refused by the storage guard
    ``initiate_oauth_connection`` installs."""
    coordinator = PendingAuthCoordinator(b"k" * 32)
    pending = coordinator.create_pending("default", "mixpanel", DEV)
    await pending.redirect_handler(
        "https://idp.example.invalid/authorize?state=upstream-state",
    )
    assert coordinator.complete_by_key(
        "default", "mixpanel", DEV, "code", "upstream-state",
    ) is pending

    assert coordinator.abort_for_user("default", DEV) == 1

    with pytest.raises(SignInAborted):
        await pending.callback_handler()
    store = FileConnectionStore(tmp_path)
    storage = McpTokenStorage(
        store, "default", "mixpanel", DEV,
        fresh_sign_in_refusal=pending.check_sign_in,
    )
    storage.mark_fresh_sign_in()
    await storage.set_tokens(SdkToken(
        access_token="ex-member", token_type="Bearer",
        refresh_token="ex-member-refresh", expires_in=3600,
    ))
    assert await store.get_user_token("default", DEV, "mixpanel") is None


def test_the_upstream_callback_refuses_a_member_removed_meanwhile(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    pending = coordinator_of(client).create_pending("default", "mixpanel", DEV)
    state = asyncio.run(signed_state_for(pending))
    client.delete("/api/admin/users/dev%40example.com")
    # A flow left over in the table (say, recreated by a stale tab) is
    # refused at the callback all the same.
    leftover = coordinator_of(client).create_pending("default", "mixpanel", DEV)

    resp = client.get(
        "/api/oauth/upstream/callback",
        params={"code": "code-from-upstream", "state": state},
    )

    assert "not a member of this organization" in resp.text
    assert leftover.aborted
    assert leftover.auth_code is None
    store = FileConnectionStore(tmp_path / "data")
    assert asyncio.run(
        store.pop_pending_code("default", "mixpanel", DEV),
    ) is None


def test_the_upstream_callback_completes_a_members_sign_in(tmp_path: Path) -> None:
    client = make_test_client(tmp_path)
    pending = coordinator_of(client).create_pending("default", "mixpanel", DEV)
    state = asyncio.run(signed_state_for(pending))

    resp = client.get(
        "/api/oauth/upstream/callback",
        params={"code": "code-from-upstream", "state": state},
    )

    assert "Authorization successful" in resp.text
    assert pending.auth_code == "code-from-upstream"


def test_flows_nobody_completes_are_forgotten_once_their_link_expired() -> None:
    now = [1000.0]
    coordinator = PendingAuthCoordinator(b"k" * 32, monotonic=lambda: now[0])
    coordinator.create_pending("default", "mixpanel", DEV)

    now[0] += STATE_TOKEN_MAX_AGE - 1
    assert coordinator.get_pending("default", "mixpanel", DEV) is not None
    now[0] += 2
    assert coordinator.get_pending("default", "mixpanel", DEV) is None


# --- A sign-in under way, end to end, when its member is removed ---


class MetadataWriteHeld(FileConnectionStore):
    """Its ``put_oauth_metadata`` waits for ``release``: the writes that
    follow a fresh sign-in's saved tokens are then under way."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def put_oauth_metadata(
        self, org_id: str, upstream_id: str, user_id: str,
        metadata: dict[str, Any],
    ) -> None:
        self.reached.set()
        await self.release.wait()
        await super().put_oauth_metadata(org_id, upstream_id, user_id, metadata)


async def connect_and_hand_the_code_over(
    store: FileConnectionStore,
    coordinator: PendingAuthCoordinator,
    upstream: UpstreamDefinition,
) -> UpstreamClientManager:
    """DEV clicks Connect and follows the sign-in link; the upstream's
    callback hands the code over. The sign-in library goes on exchanging
    it in the background."""
    manager = UpstreamClientManager([upstream])
    result = await initiate_oauth_connection(
        DEFAULT_ORG_ID, upstream, DEV, store, coordinator, manager,
        "http://localhost:8000",
    )
    assert result.authorization_url, result.error
    pending = coordinator.get_pending(DEFAULT_ORG_ID, upstream.id, DEV)
    assert pending is not None
    assert coordinator.complete_by_key(
        DEFAULT_ORG_ID, upstream.id, DEV, "the-code", pending.auth_state,
    ) is pending
    return manager


async def remove_dev(
    store: FileConnectionStore, coordinator: PendingAuthCoordinator,
) -> None:
    """What removing DEV from the org does to their sign-ins
    (``UserAdminService._end_membership``)."""
    coordinator.abort_for_user(DEFAULT_ORG_ID, DEV)
    await store.delete_all_for_user(DEFAULT_ORG_ID, DEV)


async def test_a_removal_during_the_code_exchange_saves_no_sign_in(
    tmp_path: Path,
) -> None:
    """Through the real flow: the guard ``initiate_oauth_connection``
    puts on its storage is what keeps the code exchanged after the
    removal from landing."""
    fake = FakeOAuthUpstream(hold_token_exchange=True)
    server, task = await start_fake_oauth_upstream(fake)
    store = FileConnectionStore(tmp_path)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    upstream = make_oauth_protected_upstream(fake.base)
    manager = await connect_and_hand_the_code_over(store, coordinator, upstream)
    try:
        assert await asyncio.to_thread(fake.token_requested.wait, 10)

        await remove_dev(store, coordinator)
        fake.release_token.set()
        await ucs._sign_in_waits.drain(10)  # pyright: ignore[reportPrivateUsage]

        assert await store.get_user_token(DEFAULT_ORG_ID, DEV, upstream.id) is None
    finally:
        await manager.disconnect_all_user_sessions(DEV)
        await stop_upstream(server, task)


async def test_a_removal_right_after_the_token_save_leaves_no_oauth_state(
    tmp_path: Path,
) -> None:
    """The removal lands after the tokens were saved, while the writes
    that follow them are under way: none of them may outlive it (an app
    registration a re-invite would reuse, server metadata)."""
    fake = FakeOAuthUpstream()
    server, task = await start_fake_oauth_upstream(fake)
    store = MetadataWriteHeld(tmp_path)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    upstream = make_oauth_protected_upstream(fake.base)
    manager = await connect_and_hand_the_code_over(store, coordinator, upstream)
    try:
        await asyncio.wait_for(store.reached.wait(), 10)
        assert await store.get_user_token(DEFAULT_ORG_ID, DEV, upstream.id) is not None

        await remove_dev(store, coordinator)
        store.release.set()
        await ucs._sign_in_waits.drain(10)  # pyright: ignore[reportPrivateUsage]

        leftovers = {
            "token": await store.get_user_token(DEFAULT_ORG_ID, DEV, upstream.id),
            "client_info": await store.get_client_info(DEFAULT_ORG_ID, upstream.id, DEV),
            "oauth_metadata": await store.get_oauth_metadata(
                DEFAULT_ORG_ID, upstream.id, DEV,
            ),
        }
        assert leftovers == {"token": None, "client_info": None, "oauth_metadata": None}
    finally:
        await manager.disconnect_all_user_sessions(DEV)
        await stop_upstream(server, task)


class TokenSaveHeld(FileConnectionStore):
    """Its ``put_user_token`` (a fresh sign-in's save) waits for
    ``release``: the sign-in's last check already passed."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def put_user_token(
        self, org_id: str, user_id: str, upstream_id: str, token: OAuthToken,
    ) -> SavedSignIn:
        self.reached.set()
        await self.release.wait()
        return await super().put_user_token(org_id, user_id, upstream_id, token)


async def test_a_sign_in_saved_after_its_members_removal_is_deleted(
    tmp_path: Path,
) -> None:
    """The removal lands while the sign-in's tokens are being saved (its
    check passed just before): they land after the removal's clean-up,
    and the called-off flow deletes them."""
    fake = FakeOAuthUpstream()
    server, task = await start_fake_oauth_upstream(fake)
    store = TokenSaveHeld(tmp_path)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    upstream = make_oauth_protected_upstream(fake.base)
    manager = await connect_and_hand_the_code_over(store, coordinator, upstream)
    try:
        await asyncio.wait_for(store.reached.wait(), 10)

        await remove_dev(store, coordinator)
        store.release.set()
        await ucs._sign_in_waits.drain(10)  # pyright: ignore[reportPrivateUsage]

        assert await store.get_user_token(DEFAULT_ORG_ID, DEV, upstream.id) is None
    finally:
        store.release.set()
        await manager.disconnect_all_user_sessions(DEV)
        await stop_upstream(server, task)


async def test_a_called_off_sign_in_leaves_the_new_sign_in_of_a_member_invited_again(
    tmp_path: Path,
) -> None:
    """DEV is removed while their sign-in's code is exchanged, invited
    again, and signs in anew before the old sign-in's flow ends. That
    flow's clean-up used to delete whatever sign-in DEV had stored: the
    new one, and with it the app registration it refreshes with."""
    fake = FakeOAuthUpstream(hold_token_exchange=True)
    server, task = await start_fake_oauth_upstream(fake)
    store = FileConnectionStore(tmp_path)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    upstream = make_oauth_protected_upstream(fake.base)
    manager = await connect_and_hand_the_code_over(store, coordinator, upstream)
    new_registration = {"client_id": "c-new", "redirect_uris": ["http://localhost:8000/cb"]}
    try:
        assert await asyncio.to_thread(fake.token_requested.wait, 10)

        await remove_dev(store, coordinator)
        new_sign_in = await store.put_user_token(
            DEFAULT_ORG_ID, DEV, upstream.id, make_live_token("new-sign-in"),
        )
        await store.put_client_info(DEFAULT_ORG_ID, upstream.id, DEV, new_registration)
        fake.release_token.set()
        await ucs._sign_in_waits.drain(10)  # pyright: ignore[reportPrivateUsage]

        kept = await store.get_user_token(DEFAULT_ORG_ID, DEV, upstream.id)
        assert kept is not None and kept.revision == new_sign_in.revision
        assert await store.get_client_info(
            DEFAULT_ORG_ID, upstream.id, DEV,
        ) == new_registration
    finally:
        await manager.disconnect_all_user_sessions(DEV)
        await stop_upstream(server, task)


# --- The periodic refresh and the warner leave non-members alone ---


async def test_the_periodic_refresh_skips_sign_ins_of_non_members(
    tmp_path: Path,
) -> None:
    store = FileConnectionStore(tmp_path)
    upstream = make_oauth_upstream(id="notion", mode=AuthMode.per_user_oauth)
    for user in (DEV, "removed@example.com"):
        await store.put_user_token(DEFAULT_ORG_ID, user, "notion", make_live_token(user))

    with structlog.testing.capture_logs() as logs:
        await refresh_org_sign_ins(
            DEFAULT_ORG_ID, [upstream], store, "http://localhost:8000",
            is_member=lambda email: email == DEV,
            is_stopped=lambda _upstream_id: False,
        )

    skipped = [
        e["user"] for e in logs
        if e["event"] == "oauth.token.refresh.skipped.not_a_member"
    ]
    looked_at = [
        e["user"] for e in logs
        if e["event"] == "oauth.token.refresh.skipped.not_needed"
    ]
    assert skipped == ["removed@example.com"]
    assert looked_at == [DEV]


async def test_the_periodic_refresh_skips_sign_ins_to_a_stopped_mcp(
    tmp_path: Path,
) -> None:
    """Stop keeps every sign-in for Start, and nobody can use them
    meanwhile: the gateway leaves the stopped MCP alone, so a refusal
    cannot delete a sign-in Stop promised to keep."""
    store = FileConnectionStore(tmp_path)
    upstreams = [
        make_oauth_upstream(id=upstream_id, mode=AuthMode.per_user_oauth)
        for upstream_id in ("notion", "linear")
    ]
    for upstream in upstreams:
        await store.put_user_token(
            DEFAULT_ORG_ID, DEV, upstream.id, make_live_token(upstream.id),
        )

    with structlog.testing.capture_logs() as logs:
        await refresh_org_sign_ins(
            DEFAULT_ORG_ID, upstreams, store, "http://localhost:8000",
            is_member=lambda _email: True,
            is_stopped=lambda upstream_id: upstream_id == "linear",
        )

    looked_at = [
        e["upstream_id"] for e in logs
        if e["event"] == "oauth.token.refresh.skipped.not_needed"
    ]
    assert looked_at == ["notion"]


def refreshes_looked_at(logs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """User → what the periodic refresh did with their sign-in."""
    outcomes = {
        "oauth.token.refresh.skipped.not_needed": "looked_at",
        "oauth.token.refresh.skipped.not_a_member": "not_a_member",
        "oauth.token.refresh.skipped.locked": "locked",
    }
    return {
        e["user"]: outcomes[e["event"]] for e in logs
        if e["event"] in outcomes
    }


async def test_the_apps_periodic_refresh_reads_the_orgs_members_and_refresh_lock(
    tmp_path: Path,
) -> None:
    """The app's round of the periodic refresh is wired to the org's
    running policy (who is a member) and to the refresh lock its
    reconnects take: a sign-in a reconnect is refreshing right now is
    left to it."""
    client = make_test_client(tmp_path)
    manager: OrgRuntimeManager = client.app.state.runtime_manager  # type: ignore[attr-defined]
    store: FileConnectionStore = client.app.state.connection_store  # type: ignore[attr-defined]
    runtime = await manager.get(DEFAULT_ORG_ID)
    for user in (DEV, ADMIN, "removed@example.com"):
        await store.put_user_token(DEFAULT_ORG_ID, user, "mixpanel", make_live_token(user))
    lock = runtime.client_manager.sign_in_refresh_lock

    async with lock.hold(DEFAULT_ORG_ID, "mixpanel", ADMIN):
        with structlog.testing.capture_logs() as logs:
            await _refresh_sign_ins_of_org(
                runtime, store, "http://localhost:8000", warner=None,
            )

    assert refreshes_looked_at(logs) == {
        DEV: "looked_at",
        ADMIN: "locked",
        "removed@example.com": "not_a_member",
    }


async def test_the_apps_periodic_refresh_leaves_a_stopped_mcp_alone(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    manager: OrgRuntimeManager = client.app.state.runtime_manager  # type: ignore[attr-defined]
    store: FileConnectionStore = client.app.state.connection_store  # type: ignore[attr-defined]
    runtime = await manager.get(DEFAULT_ORG_ID)
    await store.put_user_token(DEFAULT_ORG_ID, DEV, "mixpanel", make_live_token("d"))
    runtime.client_manager.mark_saved_stops({"mixpanel"})

    with structlog.testing.capture_logs() as logs:
        await _refresh_sign_ins_of_org(
            runtime, store, "http://localhost:8000", warner=None,
        )

    assert refreshes_looked_at(logs) == {}


async def test_the_warner_emails_nobody_about_a_non_members_sign_in() -> None:
    sender = StubEmailSender()
    warner = make_sign_in_warner(
        sender, admins=[ADMIN], non_members={"removed@example.com"},
    )
    upstream = make_oauth_upstream(id="notion", mode=AuthMode.per_user_oauth)

    sent = await warner.send(
        org_id=DEFAULT_ORG_ID, upstream=upstream, user_id="removed@example.com",
    )
    warner.warn_deleted(
        org_id=DEFAULT_ORG_ID, upstream=upstream, user_id="removed@example.com",
    )
    await warner.drain()

    assert sent == 0
    assert sender.sent == []


async def test_the_warner_emails_nobody_about_a_stopped_mcp() -> None:
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, admins=[ADMIN], stopped={"notion"})

    for mode, user in (
        (AuthMode.admin_oauth, ADMIN), (AuthMode.per_user_oauth, DEV),
    ):
        upstream = make_oauth_upstream(id="notion", mode=mode)
        warner.warn_deleted(org_id=DEFAULT_ORG_ID, upstream=upstream, user_id=user)
    await warner.drain()

    assert sender.sent == []


async def test_the_warner_compares_admin_addresses_ignoring_letter_case() -> None:
    """An admin's own per-user sign-in, saved under another spelling of
    their address, still gets the admin page link (My Tools has no
    sign-in button while no admin sign-in is left)."""
    sender = StubEmailSender()
    warner = make_sign_in_warner(sender, admins=[ADMIN])
    upstream = make_oauth_upstream(id="notion", mode=AuthMode.per_user_oauth)

    await warner.send(
        org_id=DEFAULT_ORG_ID, upstream=upstream, user_id="Admin@Example.com",
    )

    assert len(sender.sent) == 1
    assert "/admin/upstream/notion" in sender.sent[0].body_text


async def test_the_apps_org_facts_see_a_sign_in_a_stop_kept(tmp_path: Path) -> None:
    """Stop keeps the admin sign-in for Start to reuse: "an admin is
    still signed in" holds while stopped, though the MCP isn't Ready.
    Membership comes from the org's running policy."""
    client = make_test_client(tmp_path)
    manager: OrgRuntimeManager = client.app.state.runtime_manager  # type: ignore[attr-defined]
    store: FileConnectionStore = client.app.state.connection_store  # type: ignore[attr-defined]
    facts = _RuntimeOrgFacts(manager, store)
    upstream = make_oauth_upstream(id="mixpanel", mode=AuthMode.per_user_oauth)
    await store.put_user_token(DEFAULT_ORG_ID, ADMIN, "mixpanel", make_live_token("a"))
    runtime = await manager.get(DEFAULT_ORG_ID)
    runtime.client_manager.mark_saved_stops({"mixpanel"})

    assert await facts.is_stopped(DEFAULT_ORG_ID, "mixpanel")
    assert await facts.has_admin_sign_in(DEFAULT_ORG_ID, upstream)
    assert await facts.is_member(DEFAULT_ORG_ID, DEV)
    assert not await facts.is_member(DEFAULT_ORG_ID, "stranger@elsewhere.example")
