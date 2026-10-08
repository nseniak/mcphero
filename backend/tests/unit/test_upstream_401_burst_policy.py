"""A short burst of 401s from an upstream (its auth backend hiccuping, a
redeploy) must not delete a sign-in whose refresh token works.

The MCP SDK answers a 401 on a request it sent with a valid bearer by
starting the authorization_code grant, never the refresh_token grant,
and the silent reconnect used to read that as a dead sign-in: delete it
at once and email the member to sign in again. A reconnect now forces
one refresh first and acts on the upstream's real answer:

- no answer to act on (no token endpoint, a 5xx, network trouble) →
  keep the sign-in, count a transient failure, email nobody;
- ``invalid_grant`` → delete and warn, as for any refused refresh;
- new tokens → connect again with them.

A 401 right after a refresh the upstream accepted (the reconnect's own,
or the periodic refresh's) is a burst too. An upstream that keeps
refusing even the bearers it just issued gets a forced refresh only on a
backoff schedule, not one per tool call, and once the transient
threshold deletes the sign-in, the member is told to sign in again.

Real loopback MCP server answering 401 to its first MCP POSTs (the
reconnect's token probe and its connect), real file store, real
reconnect path, a recording warner.

NOTE: no ``from __future__ import annotations`` (FastMCP, see harness).
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcpolis.adapters.repositories.connection_store import OAuthToken as StoredToken
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
# ``upstream_connection_service`` first: it and ``oauth_refresh`` import
# each other, and only that order resolves.
from mcpolis.domain.services.upstream_connection_service import (
    MAX_CONSECUTIVE_TRANSIENT_FAILURES,
    MIN_TRANSIENT_FAILURE_WINDOW_SECONDS,
)
from mcpolis.domain.services.oauth_refresh import refresh_token_for_user
from tests.unit._loopback_mcp import free_port
from tests.unit._user_session_harness import (
    ALICE,
    GATEWAY_URL,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_upstream,
    make_upstream_server,
    stop_upstream,
    wait_until_serving,
)
from tests.unit.factories import make_oauth_metadata, seed_refresh_failure_streak

TokenAnswer = tuple[int, dict[str, object]]

GOOD_REFRESH: TokenAnswer = (200, {
    "access_token": "new-bearer", "token_type": "Bearer",
    "expires_in": 3600, "refresh_token": "new-refresh",
})
# An upstream that issues tokens with no lifetime (no ``expires_in``).
NO_EXPIRY_REFRESH: TokenAnswer = (200, {
    "access_token": "new-bearer", "token_type": "Bearer",
    "refresh_token": "new-refresh",
})
# An upstream whose refresh renews only the refresh token: the bearer it
# hands back is the one already stored (``make_signed_in_store``).
SAME_BEARER_REFRESH: TokenAnswer = (200, {
    "access_token": "good-bearer", "token_type": "Bearer",
    "expires_in": 3600, "refresh_token": "new-refresh",
})
# Refuses more MCP requests than any test makes.
ALWAYS = 10_000


class FlakyAuthUpstream:
    """Answers the first ``refusals`` MCP POSTs with 401, then passes.
    ``/token`` (where a refresh goes when the upstream published no
    OAuth metadata) answers ``token_answer`` when given, else falls
    through to the MCP app (404)."""

    def __init__(
        self, app: ASGIApp, refusals: int, token_answer: TokenAnswer | None,
    ) -> None:
        self.app = app
        self.left = refusals
        self.refused = 0
        self.token_answer = token_answer
        self.refresh_requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            path = scope["path"].rstrip("/")
            if path == "/token" and self.token_answer is not None:
                self.refresh_requests += 1
                status, body = self.token_answer
                await self._answer(send, status, body)
                return
            if path == "/mcp" and self.left > 0:
                self.left -= 1
                self.refused += 1
                await self._answer(
                    send, 401, {},
                    extra=[(b"www-authenticate", b'Bearer error="invalid_token"')],
                )
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _answer(
        send: Send,
        status: int,
        body: dict[str, object],
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        start: Message = {
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"application/json"), *(extra or [])],
        }
        await send(start)
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class RecordingWarner:
    def __init__(self) -> None:
        self.warned: list[tuple[str, str, str]] = []

    def warn_deleted(
        self, *, org_id: str, upstream: UpstreamDefinition, user_id: str,
    ) -> None:
        self.warned.append((org_id, upstream.id, user_id))


def make_flaky_upstream(
    token_answer: TokenAnswer | None, *, refusals: int = 2,
) -> FlakyAuthUpstream:
    return FlakyAuthUpstream(
        make_upstream_server(ConnectionGate()).streamable_http_app(),
        refusals=refusals, token_answer=token_answer,
    )


async def serve(
    wrapper: FlakyAuthUpstream,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    """Serve ``wrapper`` on loopback; returns its MCP URL last."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        wrapper, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    await wait_until_serving(port)
    return server, task, f"http://127.0.0.1:{port}/mcp"


async def start_flaky_upstream(
    token_answer: TokenAnswer | None, *, refusals: int = 2,
) -> tuple[uvicorn.Server, asyncio.Task[None], str, FlakyAuthUpstream]:
    wrapper = make_flaky_upstream(token_answer, refusals=refusals)
    server, task, url = await serve(wrapper)
    return server, task, url, wrapper


async def make_signed_in_store(
    tmp_path: Path, *, expires_in: timedelta = timedelta(hours=1),
) -> FileConnectionStore:
    """ALICE's sign-in (with a refresh token), its bearer valid for
    ``expires_in``, and the app registration a real sign-in leaves
    behind."""
    store = FileConnectionStore(tmp_path)
    await store.put_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, StoredToken(
        access_token="good-bearer",
        refresh_token="good-refresh",
        expires_at=datetime.now(UTC) + expires_in,
        scopes=[],
    ))
    await store.put_client_info(
        DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        {"client_id": "c-1", "redirect_uris": ["http://localhost:8000/cb"]},
    )
    return store


def make_manager(url: str) -> tuple[UpstreamClientManager, RecordingWarner]:
    manager = UpstreamClientManager([make_upstream(url)])
    warner = RecordingWarner()
    manager.set_sign_in_warner(warner)  # type: ignore[arg-type]
    return manager, warner


async def test_two_transient_401s_keep_a_sign_in_that_has_a_refresh_token(
    tmp_path: Path,
) -> None:
    server, task, url, wrapper = await start_flaky_upstream(token_answer=None)
    upstream = make_upstream(url)
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)

        with pytest.raises(Exception):
            await acquire(mgr, upstream, store)

        assert wrapper.refused == 2
        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is not None
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_refresh_the_upstream_refuses_deletes_the_sign_in_and_warns(
    tmp_path: Path,
) -> None:
    server, task, url, wrapper = await start_flaky_upstream(
        token_answer=(400, {"error": "invalid_grant"}),
    )
    upstream = make_upstream(url)
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)

        with pytest.raises(Exception):
            await acquire(mgr, upstream, store)

        assert wrapper.refresh_requests == 1
        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is None
        assert warner.warned == [(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)]
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_refresh_that_works_reconnects_with_the_new_tokens(
    tmp_path: Path,
) -> None:
    server, task, url, wrapper = await start_flaky_upstream(GOOD_REFRESH)
    upstream = make_upstream(url)
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)

        session = await acquire(mgr, upstream, store)

        result = await session.call_tool("whoami", {})
        assert "new-bearer" in str(result.content)
        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.access_token == "new-bearer"
        assert wrapper.refresh_requests == 1
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


# --- The forced refresh gets no real answer: transient ---


async def test_a_forced_refresh_answered_with_503_keeps_the_sign_in(
    tmp_path: Path,
) -> None:
    server, task, url, _wrapper = await start_flaky_upstream((503, {}))
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)

        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is not None
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_forced_refresh_with_no_answer_keeps_the_sign_in(
    tmp_path: Path,
) -> None:
    server, task, url, _wrapper = await start_flaky_upstream((200, {}))
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        dead_port = free_port()  # nothing listens there
        await store.put_oauth_metadata(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
            make_oauth_metadata(
                issuer=f"http://127.0.0.1:{dead_port}",
                token_endpoint=f"http://127.0.0.1:{dead_port}/token",
            ).model_dump(mode="json"),
        )

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)

        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is not None
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


# --- A 401 right after a refresh the upstream accepted: transient ---


async def test_a_401_right_after_a_refresh_that_worked_keeps_the_sign_in(
    tmp_path: Path,
) -> None:
    """The bearer is due, so the reconnect refreshes it first, and the
    upstream accepts. Its next two MCP requests (the probe, the connect)
    still get a 401: the refresh token was just proven to work, so this
    is a burst, not a dead sign-in."""
    server, task, url, wrapper = await start_flaky_upstream(GOOD_REFRESH)
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path, expires_in=timedelta(minutes=5))

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)

        assert wrapper.refresh_requests == 1
        kept = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert kept is not None and kept.access_token == "new-bearer"
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_the_periodic_refresh_keeps_a_sign_in_it_just_refreshed(
    tmp_path: Path,
) -> None:
    """The periodic refresh renews a due bearer (the upstream accepts,
    with no lifetime on the new tokens), then its probe with the new
    bearer meets one 401. That used to delete the sign-in it had just
    renewed, and email the member."""
    server, task, url, wrapper = await start_flaky_upstream(
        NO_EXPIRY_REFRESH, refusals=1,
    )
    warner = RecordingWarner()
    try:
        store = await make_signed_in_store(tmp_path, expires_in=timedelta(minutes=5))

        with structlog.testing.capture_logs() as logs:
            await refresh_token_for_user(
                DEFAULT_ORG_ID, make_upstream(url), ALICE, store, GATEWAY_URL,
                warner=warner,  # type: ignore[arg-type]
            )

        assert wrapper.refresh_requests == 1
        kept = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert kept is not None and kept.access_token == "new-bearer"
        assert warner.warned == []
        events = [e["event"] for e in logs]
        assert "oauth.token.refresh.success" in events
        assert "oauth.token.refresh.synthesized_invalid_grant" not in events
    finally:
        await stop_upstream(server, task)


async def test_a_periodic_refresh_issuing_tokens_with_no_lifetime_is_a_success(
    tmp_path: Path,
) -> None:
    """A success clears the failures counted against the sign-in, so
    earlier ones cannot add up to deleting it. A refresh whose new tokens
    had no ``expires_in`` was not counted as one."""
    server, task, url, _wrapper = await start_flaky_upstream(
        NO_EXPIRY_REFRESH, refusals=0,
    )
    try:
        store = await make_signed_in_store(tmp_path, expires_in=timedelta(minutes=5))
        await seed_failures_up_to_the_threshold(store)

        await refresh_token_for_user(
            DEFAULT_ORG_ID, make_upstream(url), ALICE, store, GATEWAY_URL,
        )

        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        ) is None
    finally:
        await stop_upstream(server, task)


async def test_a_periodic_refresh_handing_back_the_same_bearer_is_a_success(
    tmp_path: Path,
) -> None:
    """The refresh saved new tokens, though the bearer in them did not
    change: a success, which clears the failures counted against the
    sign-in. Judged by a changed bearer alone, it counted as nothing
    having happened."""
    server, task, url, _wrapper = await start_flaky_upstream(
        SAME_BEARER_REFRESH, refusals=0,
    )
    try:
        store = await make_signed_in_store(tmp_path, expires_in=timedelta(minutes=5))
        await seed_failures_up_to_the_threshold(store)

        with structlog.testing.capture_logs() as logs:
            await refresh_token_for_user(
                DEFAULT_ORG_ID, make_upstream(url), ALICE, store, GATEWAY_URL,
            )

        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None
        assert (stored.access_token, stored.refresh_token) == (
            "good-bearer", "new-refresh",
        )
        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        ) is None
        assert "oauth.token.refresh.success" in [e["event"] for e in logs]
    finally:
        await stop_upstream(server, task)


# --- An upstream that keeps refusing the bearers it issues ---


async def test_an_upstream_that_keeps_refusing_is_not_refreshed_once_per_reconnect(
    tmp_path: Path,
) -> None:
    """It refuses even the bearer it just issued (wrong audience after a
    URL change, the account lost access): each tool call's reconnect used
    to spend a refresh grant on it."""
    server, task, url, wrapper = await start_flaky_upstream(
        GOOD_REFRESH, refusals=ALWAYS,
    )
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        for _ in range(3):  # three tool calls, each needing a session
            with pytest.raises(Exception):
                await acquire(mgr, make_upstream(url), store)

        assert wrapper.refresh_requests == 1
        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is not None
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_session_that_works_again_lets_the_next_401_force_a_refresh_at_once(
    tmp_path: Path,
) -> None:
    server, task, url, wrapper = await start_flaky_upstream(GOOD_REFRESH)
    upstream = make_upstream(url)
    mgr, _warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        await acquire(mgr, upstream, store)
        assert wrapper.refresh_requests == 1
        await mgr.disconnect_all_user_sessions(ALICE)
        wrapper.left = 2  # another burst of 401s

        session = await acquire(mgr, upstream, store)

        assert wrapper.refresh_requests == 2
        result = await session.call_tool("whoami", {})
        assert "new-bearer" in str(result.content)
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_reconnect_that_works_without_forcing_a_refresh_ends_the_backoff(
    tmp_path: Path,
) -> None:
    """A forced refresh after which the session was still refused starts
    the backoff. The upstream then recovers, and a reconnect works with
    no forced refresh: the next burst of 401s may force one at once again,
    instead of a minute later (a tool call refused meanwhile)."""
    # The reconnect's probe and connect, the forced refresh's probe, and
    # the connect after it.
    server, task, url, wrapper = await start_flaky_upstream(GOOD_REFRESH, refusals=4)
    upstream = make_upstream(url)
    mgr, _warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        with pytest.raises(Exception):
            await acquire(mgr, upstream, store)
        assert wrapper.refresh_requests == 1

        await acquire(mgr, upstream, store)
        assert wrapper.refresh_requests == 1
        await mgr.disconnect_all_user_sessions(ALICE)
        wrapper.left = 2  # another burst of 401s

        session = await acquire(mgr, upstream, store)

        assert wrapper.refresh_requests == 2
        result = await session.call_tool("whoami", {})
        assert "new-bearer" in str(result.content)
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_two_concurrent_reconnects_share_one_forced_refresh(
    tmp_path: Path,
) -> None:
    server, task, url, wrapper = await start_flaky_upstream(GOOD_REFRESH)
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        upstream = make_upstream(url)

        outcomes = await asyncio.gather(
            acquire(mgr, upstream, store), acquire(mgr, upstream, store),
            return_exceptions=True,
        )

        assert wrapper.refresh_requests == 1, outcomes
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def seed_failures_up_to_the_threshold(store: FileConnectionStore) -> None:
    """One more failure deletes the sign-in."""
    await seed_refresh_failure_streak(
        store,
        upstream_id=UPSTREAM_ID,
        user_id=ALICE,
        failures=MAX_CONSECUTIVE_TRANSIENT_FAILURES - 1,
        started_ago=timedelta(seconds=MIN_TRANSIENT_FAILURE_WINDOW_SECONDS + 60),
    )


async def test_a_sign_in_the_upstream_keeps_refusing_is_deleted_with_an_email(
    tmp_path: Path,
) -> None:
    """At the transient threshold the sign-in is deleted. The upstream
    refuses the bearer while accepting the refresh token: only signing in
    again brings it back, so the member is told, as before the forced
    refresh existed (a silent sign-out since)."""
    server, task, url, wrapper = await start_flaky_upstream(
        GOOD_REFRESH, refusals=ALWAYS,
    )
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        await seed_failures_up_to_the_threshold(store)

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)

        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is None
        assert warner.warned == [(DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)]
        assert wrapper.refresh_requests == 1
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)


async def test_a_threshold_reached_while_the_token_endpoint_fails_sends_no_email(
    tmp_path: Path,
) -> None:
    """The bearer is refused and the refresh gets a 503: an outage, in
    which signing in again cannot work either. The threshold deletes the
    sign-in without telling the member to sign in again."""
    server, task, url, _wrapper = await start_flaky_upstream(
        (503, {}), refusals=ALWAYS,
    )
    mgr, warner = make_manager(url)
    try:
        store = await make_signed_in_store(tmp_path)
        await seed_failures_up_to_the_threshold(store)

        with pytest.raises(Exception):
            await acquire(mgr, make_upstream(url), store)

        assert await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID) is None
        assert warner.warned == []
    finally:
        await mgr.disconnect_all_user_sessions(ALICE)
        await stop_upstream(server, task)
