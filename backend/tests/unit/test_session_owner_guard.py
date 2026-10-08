"""``SessionOwnerGuard`` in isolation: who owns a session, and the answer
everyone else gets.

The inner app stands in for the SDK. A request without a session id
opens a session: added to the session manager, its ``mcp-session-id``
on the answer, which is 200 for a POST and 400 otherwise (the SDK opens
a session for a GET without an id, then refuses the GET). A DELETE
ends the session like the SDK does (mcp 1.30): terminated, removed from
the manager.
Any other request is answered "handled". The
caller and org are set the way the auth and org middleware set them,
through ``auth_context_var`` and ``current_org_id``. The full-app tests
(``test_gateway_session_owner.py``, ``test_cloud_mcp_session_owner.py``)
pin the wiring on the real endpoints.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcpolis.domain.model.service_token import generate_service_token
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id
from mcpolis.entrypoints.middleware.session_owner_guard import (
    IDLE_SWEEP_INTERVAL_SECONDS,
    SESSION_IDLE_LIMIT_SECONDS,
    SessionOwnerGuard,
    session_not_found,
)

SESSION = "a" * 32
OTHER_SESSION = "b" * 32
ACME = "acme-id"
BOBCO = "bobco-id"


class Answer(BaseModel):
    status: int
    session_id: str | None
    body: bytes


def make_caller(user_id: str, token: str = "oauth-token") -> AuthenticatedUser:
    return AuthenticatedUser(
        AccessToken(token=token, client_id=user_id, scopes=[], expires_at=None),
    )


def make_session_manager() -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(app=Server("guard-test"))


def make_sdk_stand_in(
    session_manager: StreamableHTTPSessionManager,
    opens: list[str],
    opened: list[StreamableHTTPServerTransport] | None = None,
    stream_open: asyncio.Event | None = None,
) -> ASGIApp:
    """Each request without a session id opens the next id of ``opens``
    (its transport is appended to ``opened``). A GET on a session is an
    event stream, open until ``stream_open`` is set."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        session_id = Headers(scope=scope).get("mcp-session-id")
        if session_id is None:
            session_id = opens.pop(0)
            transport = StreamableHTTPServerTransport(mcp_session_id=session_id)
            session_manager._server_instances[session_id] = transport
            if opened is not None:
                opened.append(transport)
            response = Response(
                "opened" if scope["method"] == "POST" else "refused",
                status_code=200 if scope["method"] == "POST" else 400,
                headers={"mcp-session-id": session_id},
            )
        elif scope["method"] == "DELETE":
            await session_manager._server_instances.pop(session_id).terminate()
            response = Response("ended")
        elif scope["method"] == "GET" and stream_open is not None:
            await stream_open.wait()
            response = Response("stream closed")
        else:
            response = Response("handled")
        await response(scope, receive, send)

    return app


def make_guard(
    session_manager: StreamableHTTPSessionManager,
    opens: list[str] | None = None,
    *,
    ended: list[str] | None = None,
    opened: list[StreamableHTTPServerTransport] | None = None,
    clock: FakeClock | None = None,
    stream_open: asyncio.Event | None = None,
) -> SessionOwnerGuard:
    """``ended`` collects the ids the guard reports as ended."""
    return SessionOwnerGuard(
        make_sdk_stand_in(
            session_manager, opens or [SESSION], opened, stream_open,
        ),
        session_manager,
        on_session_end=ended.append if ended is not None else None,
        clock=(clock or FakeClock()).now,
    )


class FakeClock(BaseModel):
    """Monotonic seconds, moved by hand."""

    seconds: float = 1000.0

    def now(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


async def ask(
    app: ASGIApp,
    *,
    caller: AuthenticatedUser | None,
    org_id: str = ACME,
    session_id: str | None = None,
    method: str = "POST",
) -> Answer:
    """One request through ``app``, as ``caller`` on ``org_id``."""
    headers = (
        [] if session_id is None
        else [(b"mcp-session-id", session_id.encode())]
    )
    scope: Scope = {
        "type": "http", "method": method, "path": "/", "headers": headers,
        "query_string": b"",
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    auth_reset = auth_context_var.set(caller)
    org_reset = current_org_id.set(org_id)
    try:
        await app(scope, receive, send)
    finally:
        auth_context_var.reset(auth_reset)
        current_org_id.reset(org_reset)
    start_headers = Headers(raw=sent[0]["headers"])
    return Answer(
        status=sent[0]["status"],
        session_id=start_headers.get("mcp-session-id"),
        body=b"".join(m.get("body", b"") for m in sent[1:]),
    )


def not_found_answer() -> Answer:
    response = session_not_found()
    return Answer(
        status=response.status_code, session_id=None, body=bytes(response.body),
    )


@pytest.mark.asyncio
async def test_the_caller_who_opens_a_session_owns_it() -> None:
    guard = make_guard(make_session_manager())
    alice = make_caller("alice@acme.test")

    opened = await ask(guard, caller=alice)
    assert opened.session_id == SESSION

    for method in ("POST", "GET"):
        answer = await ask(
            guard, caller=alice, session_id=SESSION, method=method,
        )
        assert (answer.status, answer.body) == (200, b"handled"), method
    ended = await ask(guard, caller=alice, session_id=SESSION, method="DELETE")
    assert (ended.status, ended.body) == (200, b"ended")


@pytest.mark.asyncio
async def test_anyone_else_gets_the_unknown_session_answer() -> None:
    guard = make_guard(make_session_manager())
    await ask(guard, caller=make_caller("alice@acme.test"))
    bob = make_caller("bob@acme.test", token="bobs-token")

    for method in ("POST", "GET", "DELETE"):
        answer = await ask(guard, caller=bob, session_id=SESSION, method=method)
        assert answer == not_found_answer(), method
    unknown = await ask(guard, caller=bob, session_id=OTHER_SESSION)
    assert unknown == not_found_answer()


@pytest.mark.asyncio
async def test_a_session_belongs_to_the_org_it_was_opened_on() -> None:
    guard = make_guard(make_session_manager())
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice, org_id=ACME)

    elsewhere = await ask(guard, caller=alice, org_id=BOBCO, session_id=SESSION)
    assert elsewhere == not_found_answer()
    home = await ask(guard, caller=alice, org_id=ACME, session_id=SESSION)
    assert home.body == b"handled"


@pytest.mark.asyncio
async def test_a_person_keeps_the_session_across_access_tokens() -> None:
    guard = make_guard(make_session_manager())
    await ask(guard, caller=make_caller("alice@acme.test", token="first"))

    refreshed = await ask(
        guard,
        caller=make_caller("alice@acme.test", token="second"),
        session_id=SESSION,
    )
    assert refreshed.body == b"handled"


@pytest.mark.asyncio
async def test_a_service_token_session_belongs_to_that_token() -> None:
    """Same ``svc:<label>`` identity, different token (re-minted after a
    revoke): not the owner."""
    guard = make_guard(make_session_manager())
    first = generate_service_token()
    await ask(guard, caller=make_caller("svc:bot", token=first))

    same_token = await ask(
        guard, caller=make_caller("svc:bot", token=first), session_id=SESSION,
    )
    assert same_token.body == b"handled"
    reminted = await ask(
        guard,
        caller=make_caller("svc:bot", token=generate_service_token()),
        session_id=SESSION,
    )
    assert reminted == not_found_answer()


@pytest.mark.asyncio
async def test_a_removed_session_is_refused_and_forgotten() -> None:
    """Removed from the manager, as when a member is removed."""
    session_manager = make_session_manager()
    guard = make_guard(session_manager, opens=[SESSION, OTHER_SESSION])
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)

    del session_manager._server_instances[SESSION]
    removed = await ask(guard, caller=alice, session_id=SESSION)
    assert removed == not_found_answer()

    await ask(guard, caller=alice)  # the next session opened
    assert set(guard._owners) == {OTHER_SESSION}


@pytest.mark.asyncio
async def test_a_terminated_session_is_refused_and_forgotten() -> None:
    """Ended without going through the guard's DELETE (closed by other
    code), so still in the manager, terminated: as good as gone."""
    session_manager = make_session_manager()
    guard = make_guard(session_manager, opens=[SESSION, OTHER_SESSION])
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)
    await session_manager._server_instances[SESSION].terminate()

    reused = await ask(guard, caller=alice, session_id=SESSION)
    assert reused == not_found_answer()

    await ask(guard, caller=alice)  # the next session opened
    assert set(guard._owners) == {OTHER_SESSION}


@pytest.mark.asyncio
async def test_forgetting_ended_sessions_keeps_live_owners() -> None:
    first, second, third = "a" * 32, "b" * 32, "c" * 32
    session_manager = make_session_manager()
    guard = make_guard(session_manager, opens=[first, second, third])
    alice = make_caller("alice@acme.test")
    bob = make_caller("bob@acme.test", token="bobs-token")
    await ask(guard, caller=alice)
    await ask(guard, caller=bob)
    del session_manager._server_instances[first]
    await ask(guard, caller=alice)  # opens ``third``, forgets ``first``

    assert set(guard._owners) == {second, third}
    still_bobs = await ask(guard, caller=bob, session_id=second)
    assert still_bobs.body == b"handled"


@pytest.mark.asyncio
async def test_an_owned_session_id_keeps_its_first_owner() -> None:
    """The SDK never answers a new request with a live session's id; if
    it ever did, the session must stay with the caller who opened it."""
    guard = make_guard(make_session_manager(), opens=[SESSION, SESSION])
    alice = make_caller("alice@acme.test")
    bob = make_caller("bob@acme.test", token="bobs-token")
    await ask(guard, caller=alice)
    await ask(guard, caller=bob)

    assert (await ask(guard, caller=alice, session_id=SESSION)).body == b"handled"
    assert await ask(guard, caller=bob, session_id=SESSION) == not_found_answer()


@pytest.mark.asyncio
async def test_a_refused_open_gets_no_owner() -> None:
    """A session the SDK opened for a request it then refused (here a GET
    without an id) serves nobody, not even its caller."""
    guard = make_guard(make_session_manager())
    alice = make_caller("alice@acme.test")

    refused = await ask(guard, caller=alice, method="GET")
    assert (refused.status, refused.session_id) == (400, SESSION)
    assert guard._owners == {}
    reused = await ask(guard, caller=alice, session_id=SESSION)
    assert reused == not_found_answer()


def test_a_stateless_session_manager_is_refused() -> None:
    """No session ids to own: the guard would answer 404 to everything."""
    with pytest.raises(ValueError):
        SessionOwnerGuard(
            make_sdk_stand_in(make_session_manager(), []),
            StreamableHTTPSessionManager(app=Server("guard-test"), stateless=True),
        )


def test_the_guard_turns_off_the_sdk_idle_timer() -> None:
    """Since mcp 1.30 the SDK ends a session after 30 minutes with no
    HTTP request open, even while a tool call whose client hung up is
    still running. The guard's own idle rule waits for that call."""
    session_manager = StreamableHTTPSessionManager(app=Server("guard-test"))
    assert session_manager.session_idle_timeout is not None

    make_guard(session_manager)

    assert session_manager.session_idle_timeout is None


@pytest.mark.asyncio
async def test_a_session_opened_without_a_caller_serves_nobody() -> None:
    """Unreachable behind the auth middleware; if it ever happens the
    session is unusable rather than open to whoever presents its id."""
    guard = make_guard(make_session_manager())
    opened = await ask(guard, caller=None)
    assert opened.session_id == SESSION

    for caller in (None, make_caller("alice@acme.test")):
        answer = await ask(guard, caller=caller, session_id=SESSION)
        assert answer == not_found_answer()


@pytest.mark.asyncio
async def test_unknown_session_answer_is_the_sdks_own() -> None:
    """Before the guard, unknown ids reached the SDK; clients see the
    same 404 and body as before."""
    session_manager = make_session_manager()
    async with session_manager.run():
        sdk = await ask(
            session_manager.handle_request,
            caller=None,
            session_id=OTHER_SESSION,
        )
    assert sdk == not_found_answer()
    assert json.loads(sdk.body)["error"]["message"] == "Session not found"


# ─────────────── releasing ended sessions ───────────────


@pytest.mark.asyncio
async def test_a_deleted_session_is_released_at_once() -> None:
    """The SDK would keep a DELETEd session forever. The guard drops it as
    soon as its DELETE is answered, and reports it ended once."""
    session_manager = make_session_manager()
    ended: list[str] = []
    guard = make_guard(session_manager, ended=ended)
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)

    await ask(guard, caller=alice, session_id=SESSION, method="DELETE")

    assert SESSION not in session_manager._server_instances
    assert guard._owners == {}
    assert ended == [SESSION]


@pytest.mark.asyncio
async def test_a_refused_open_is_closed_at_once() -> None:
    """A session the SDK opened for a request it refused would run its
    tasks forever: the guard ends it as soon as the refusal is sent."""
    session_manager = make_session_manager()
    ended: list[str] = []
    opened: list[StreamableHTTPServerTransport] = []
    guard = make_guard(session_manager, ended=ended, opened=opened)

    refused = await ask(guard, caller=make_caller("alice@acme.test"), method="GET")

    assert refused.status == 400
    assert SESSION not in session_manager._server_instances
    assert [transport.is_terminated for transport in opened] == [True]
    assert ended == []  # it never had an owner, nor a registry entry


@pytest.mark.asyncio
async def test_a_session_ended_elsewhere_is_reported_when_forgotten() -> None:
    """Removed from the manager by someone else (member removal, the
    SDK's crash cleanup): reported ended when its owner is forgotten."""
    session_manager = make_session_manager()
    ended: list[str] = []
    guard = make_guard(
        session_manager, opens=[SESSION, OTHER_SESSION], ended=ended,
    )
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)
    del session_manager._server_instances[SESSION]

    await ask(guard, caller=alice)  # the next open forgets ended owners

    assert ended == [SESSION]


@pytest.mark.asyncio
async def test_a_failing_end_report_does_not_fail_the_delete() -> None:
    session_manager = make_session_manager()

    def report(_session_id: str) -> None:
        raise RuntimeError("audit store down")

    guard = SessionOwnerGuard(
        make_sdk_stand_in(session_manager, [SESSION]),
        session_manager,
        on_session_end=report,
    )
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)

    deleted = await ask(guard, caller=alice, session_id=SESSION, method="DELETE")

    assert (deleted.status, deleted.body) == (200, b"ended")
    assert SESSION not in session_manager._server_instances


# ─────────────── ending sessions nobody uses ───────────────


@pytest.mark.asyncio
async def test_a_session_idle_for_the_limit_is_ended() -> None:
    """A client that disappears without a DELETE: its session is ended
    once idle for the limit, at the next request anyone makes."""
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    opened: list[StreamableHTTPServerTransport] = []
    guard = make_guard(
        session_manager, opens=[SESSION, OTHER_SESSION],
        ended=ended, opened=opened, clock=clock,
    )
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)

    clock.advance(SESSION_IDLE_LIMIT_SECONDS)
    await ask(guard, caller=make_caller("bob@acme.test"))

    assert SESSION not in session_manager._server_instances
    assert opened[0].is_terminated
    assert ended == [SESSION]
    back = await ask(guard, caller=alice, session_id=SESSION)
    assert back == not_found_answer()


@pytest.mark.asyncio
async def test_a_session_in_use_is_kept() -> None:
    """Idle time counts from the end of the session's last request."""
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    guard = make_guard(
        session_manager, opens=[SESSION, OTHER_SESSION], ended=ended,
        clock=clock,
    )
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)
    clock.advance(SESSION_IDLE_LIMIT_SECONDS - 60)
    await ask(guard, caller=alice, session_id=SESSION)

    clock.advance(SESSION_IDLE_LIMIT_SECONDS - 60)
    await ask(guard, caller=make_caller("bob@acme.test"))

    assert ended == []
    still = await ask(guard, caller=alice, session_id=SESSION)
    assert still.body == b"handled"


@pytest.mark.asyncio
async def test_a_session_with_a_request_in_progress_is_kept() -> None:
    """An open event stream (or a long tool call) is never cut, however
    long it lasts; the session is idle only from when it ends."""
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    stream_open = asyncio.Event()
    guard = make_guard(
        session_manager, opens=[SESSION, OTHER_SESSION], ended=ended,
        clock=clock, stream_open=stream_open,
    )
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)
    stream = asyncio.create_task(
        ask(guard, caller=alice, session_id=SESSION, method="GET"),
    )
    await asyncio.sleep(0)  # the stream is open
    clock.advance(3 * SESSION_IDLE_LIMIT_SECONDS)
    await ask(guard, caller=alice, session_id=SESSION)  # sweeps meanwhile
    assert ended == []

    stream_open.set()
    assert (await stream).body == b"stream closed"
    bob = make_caller("bob@acme.test")
    clock.advance(SESSION_IDLE_LIMIT_SECONDS - IDLE_SWEEP_INTERVAL_SECONDS)
    await ask(guard, caller=bob)
    assert ended == []

    clock.advance(IDLE_SWEEP_INTERVAL_SECONDS)
    await ask(guard, caller=bob, session_id=OTHER_SESSION)
    assert ended == [SESSION]


@pytest.mark.asyncio
async def test_work_in_progress_keeps_the_session() -> None:
    """``busy`` (held by every MCP request handler) keeps the session as
    long as it lasts; idle time counts from its end."""
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    guard = make_guard(
        session_manager, opens=[SESSION, OTHER_SESSION], ended=ended,
        clock=clock,
    )
    bob = make_caller("bob@acme.test")
    await ask(guard, caller=make_caller("alice@acme.test"))

    with guard.busy(SESSION):
        clock.advance(3 * SESSION_IDLE_LIMIT_SECONDS)
        await ask(guard, caller=bob)
        assert ended == []

    clock.advance(SESSION_IDLE_LIMIT_SECONDS)
    await ask(guard, caller=bob, session_id=OTHER_SESSION)
    assert ended == [SESSION]


@pytest.mark.asyncio
async def test_a_returning_client_keeps_its_session() -> None:
    """The request that finds its own session past the limit is served:
    whether its session was ended must not depend on who swept first."""
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    guard = make_guard(session_manager, ended=ended, clock=clock)
    alice = make_caller("alice@acme.test")
    await ask(guard, caller=alice)

    clock.advance(2 * SESSION_IDLE_LIMIT_SECONDS)
    back = await ask(guard, caller=alice, session_id=SESSION)

    assert back.body == b"handled"
    assert ended == []


@pytest.mark.asyncio
async def test_idle_sessions_are_looked_for_once_a_minute_at_most() -> None:
    session_manager = make_session_manager()
    clock = FakeClock()
    ended: list[str] = []
    guard = SessionOwnerGuard(
        make_sdk_stand_in(session_manager, [SESSION, OTHER_SESSION]),
        session_manager,
        on_session_end=ended.append,
        idle_limit_seconds=IDLE_SWEEP_INTERVAL_SECONDS / 2,
        clock=clock.now,
    )
    bob = make_caller("bob@acme.test")
    await ask(guard, caller=make_caller("alice@acme.test"))
    await ask(guard, caller=bob)

    clock.advance(IDLE_SWEEP_INTERVAL_SECONDS - 1)
    await ask(guard, caller=bob, session_id=OTHER_SESSION)
    assert ended == []  # idle past the limit, but swept under a minute ago

    clock.advance(1)
    await ask(guard, caller=bob, session_id=OTHER_SESSION)
    assert ended == [SESSION]
