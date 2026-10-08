"""Every MCP session belongs to the caller that opened it.

The MCP SDK handles every request of a session inside the task it
started at ``initialize``. Handlers read the caller
(``auth_context_var``) and the org (``current_org_id``) from context
variables, so they see the values captured when the session was
opened, not the bearer and URL of the current request. A session id is
not a credential (the MCP spec: servers must verify every inbound
request and must not use sessions for authentication), and ours are
visible to org admins in ``client_connect`` audit rows and to operators
in logs. Without this guard, any signed-in caller holding someone
else's session id acted as that session's creator, in the creator's
org.

``SessionOwnerGuard`` wraps an MCP app's session manager:

- a request that opens a session records the session's owner, read off
  the ``mcp-session-id`` response header before the client receives it.
  Only a successful open (2xx) gets an owner: the SDK also opens a
  session for a request without an id that it then refuses (a GET, a
  non-``initialize`` POST), and nobody may use those;
- a request on a session from anyone but its owner (POST, the GET event
  stream, DELETE) gets exactly the answer an unknown session id gets:
  404 with the SDK's "Session not found" body. The same answer, so it
  can't reveal which session ids are live; a 404, the status the MCP
  spec tells a client to answer by starting a new session;
- an ended session is released. The SDK keeps a DELETEd session in its
  table for the life of the process, and leaves the session it opened
  for a refused request running (its tasks wait for a client that will
  never come). The guard drops the first once its DELETE is answered,
  ends the second once its refusal is sent, and reports every end of a
  session it knew the owner of to ``on_session_end``;
- a session nobody uses is ended. A client that disappears without
  closing its session (DELETE) left it, and its tasks, in memory until
  the next restart. The guard ends a session once it has had no work
  in progress for ``idle_limit_seconds`` (an hour): no HTTP request open
  on it (an event stream, a call awaiting its answer) and no MCP request
  being handled (``busy``: a tool call goes on after its client hung
  up). The SDK's own
  ``session_idle_timeout`` is not used: it counts from the last request
  received, so it would end a session in the middle of a long tool
  call. A client coming back afterwards gets the unknown-session 404
  and opens a new session, as after a restart.

The owner is the org the session was opened on, plus the caller's
identity, plus, for a service token, which token. A session opened with
a service token belongs to that token, not to its label: a token
re-minted under a revoked token's label must not inherit the session,
and with it the role captured at ``initialize``. A person owns their
session across access-token refreshes.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPStatus

import structlog
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    StreamableHTTPServerTransport,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import INVALID_REQUEST, ErrorData, JSONRPCError
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcpolis.domain.model.service_token import (
    SERVICE_TOKEN_PREFIX,
    hash_service_token,
)
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# A session with no request in progress for this long is ended.
SESSION_IDLE_LIMIT_SECONDS = 3600.0
# How often, at most, a request looks for sessions to end.
IDLE_SWEEP_INTERVAL_SECONDS = 60.0


class SessionOwner(BaseModel):
    """Who opened an MCP session, and where."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    user_id: str
    # sha256 of the service token that opened the session. None for a
    # person, who keeps the session across access-token refreshes.
    service_token_hash: str | None = None


def current_caller() -> SessionOwner | None:
    """The caller of the current request, or None if nobody signed in."""
    auth_user = auth_context_var.get(None)
    if auth_user is None:
        return None
    raw_token = auth_user.access_token.token
    # Same prefix the gateway's composite verifier dispatches on: a
    # ``svct_`` bearer only ever authenticates through the registry.
    is_service_token = raw_token.startswith(SERVICE_TOKEN_PREFIX)
    return SessionOwner(
        org_id=current_org_id.get(),
        user_id=auth_user.display_name,
        service_token_hash=(
            hash_service_token(raw_token) if is_service_token else None
        ),
    )


def session_not_found() -> Response:
    """The SDK's own answer to an unknown session id (see
    ``StreamableHTTPSessionManager._handle_stateful_request``)."""
    error = JSONRPCError(
        jsonrpc="2.0",
        id="server-error",
        error=ErrorData(code=INVALID_REQUEST, message="Session not found"),
    )
    return Response(
        content=error.model_dump_json(by_alias=True, exclude_none=True),
        status_code=HTTPStatus.NOT_FOUND,
        media_type="application/json",
    )


class SessionOwnerGuard:
    """ASGI middleware binding each session of ``session_manager`` to
    its owner. Must run after auth and org resolution: it reads
    ``auth_context_var`` and ``current_org_id`` of the current request.
    """

    def __init__(
        self,
        app: ASGIApp,
        session_manager: StreamableHTTPSessionManager,
        on_session_end: Callable[[str], None] | None = None,
        idle_limit_seconds: float = SESSION_IDLE_LIMIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if session_manager.stateless:
            # No session ids to own: every request carrying one would 404.
            raise ValueError("SessionOwnerGuard needs a stateful session manager")
        # The guard ends idle sessions itself. The SDK's own timer (on
        # by default since mcp 1.30, 30 minutes with no HTTP request
        # open) would end a session whose tool call goes on after its
        # client hung up. The SDK reads the setting when it opens a
        # session, so turning it off here covers every guarded endpoint.
        session_manager.session_idle_timeout = None
        self._app = app
        self._session_manager = session_manager
        self._on_session_end = on_session_end
        self._idle_limit_seconds = idle_limit_seconds
        self._clock = clock
        self._owners: dict[str, SessionOwner] = {}
        # Per owned session: requests in progress, and when the last one
        # ended (or the session opened).
        self._in_progress: dict[str, int] = {}
        self._idle_since: dict[str, float] = {}
        self._next_sweep = clock() + IDLE_SWEEP_INTERVAL_SECONDS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        # Read exactly as the SDK reads it (first value, any case).
        session_id = Headers(scope=scope).get(MCP_SESSION_ID_HEADER)
        caller = current_caller()
        await self._end_idle_sessions(keep=session_id)
        if session_id is None:
            await self._open(scope, receive, send, caller)
            return
        if not self._is_owner(session_id, caller):
            await session_not_found()(scope, receive, send)
            return
        try:
            with self.busy(session_id):
                await self._app(scope, receive, send)
        finally:
            if scope["method"] == "DELETE":
                self._release_if_ended(session_id)

    @contextmanager
    def busy(self, session_id: str | None) -> Iterator[None]:
        """Marks the session as having work in progress: it is not idle
        while this lasts. Held by every HTTP request on the session and,
        through ``bind_request_identity``, by every MCP request handler
        (a tool call keeps running after its client hung up, once its
        HTTP request is over)."""
        if session_id is None or session_id not in self._owners:
            yield
            return
        self._in_progress[session_id] = self._in_progress.get(session_id, 0) + 1
        try:
            yield
        finally:
            self._work_done(session_id)

    def _work_done(self, session_id: str) -> None:
        if session_id not in self._owners:
            return  # Ended meanwhile.
        left = self._in_progress.get(session_id, 1) - 1
        if left > 0:
            self._in_progress[session_id] = left
            return
        self._in_progress.pop(session_id, None)
        self._idle_since[session_id] = self._clock()

    async def _end_idle_sessions(self, keep: str | None) -> None:
        """End every owned session idle for ``idle_limit_seconds``, but
        ``keep``, the session of the request being handled.
        Runs at most once per ``IDLE_SWEEP_INTERVAL_SECONDS``, on the
        path of a request: sessions only pile up when requests come."""
        now = self._clock()
        if now < self._next_sweep:
            return
        self._next_sweep = now + IDLE_SWEEP_INTERVAL_SECONDS
        idle = [
            session_id
            for session_id, since in self._idle_since.items()
            if session_id != keep and now - since >= self._idle_limit_seconds
        ]
        for session_id in idle:
            since = self._idle_since.get(session_id)
            if (
                session_id in self._in_progress
                or since is None
                or now - since < self._idle_limit_seconds
            ):
                # Used, or ended, while an earlier session was closed.
                continue
            logger.info(
                "session.idle.ended",
                session_id_prefix=session_id[:8],
                idle_seconds=round(now - since),
            )
            await self._close(session_id, failure_event="session.idle_close.failed")
            self._forget(session_id)

    def _sessions(self) -> dict[str, StreamableHTTPServerTransport]:
        # No public API lists the SDK's sessions; PolicyNotifier reads
        # the same dict to reach them.
        return self._session_manager._server_instances  # pyright: ignore[reportPrivateUsage]

    def _is_live(self, session_id: str) -> bool:
        # A session the SDK ended is gone from its table, or (before
        # mcp 1.30, after a DELETE) still there, terminated.
        transport = self._sessions().get(session_id)
        return transport is not None and not transport.is_terminated

    def _is_owner(self, session_id: str, caller: SessionOwner | None) -> bool:
        if not self._is_live(session_id):
            # Typically a client replaying an id from before a restart,
            # or after ending its session.
            logger.info(
                "session.stale.rejected", session_id_prefix=session_id[:8],
            )
            return False
        owner = self._owners.get(session_id)
        if owner is not None and owner == caller:
            return True
        # A live session presented by someone other than its owner:
        # a leaked id being tried, or a client that switched accounts.
        logger.warning(
            "session.owner_mismatch.rejected",
            session_id_prefix=session_id[:8],
            owner_user_id=owner.user_id if owner is not None else None,
            owner_org_id=owner.org_id if owner is not None else None,
            caller_user_id=caller.user_id if caller is not None else None,
            caller_org_id=caller.org_id if caller is not None else None,
        )
        return False

    async def _open(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        caller: SessionOwner | None,
    ) -> None:
        """A request without a session id, which may open one. Records
        ``caller`` as the owner of a session the response opens, before
        the response leaves the server; ends a session the SDK opened
        for a request it refused, once the refusal is sent."""
        refused: list[str] = []

        async def send_and_record(message: Message) -> None:
            if message["type"] == "http.response.start":
                session_id = Headers(raw=message["headers"]).get(
                    MCP_SESSION_ID_HEADER,
                )
                if session_id is not None:
                    if 200 <= message["status"] < 300:
                        self._record(session_id, caller)
                    else:
                        refused.append(session_id)
            await send(message)

        try:
            await self._app(scope, receive, send_and_record)
        finally:
            for session_id in refused:
                await self._close(
                    session_id, failure_event="session.refused_close.failed",
                )

    async def _close(self, session_id: str, failure_event: str) -> None:
        """Drop the session from the SDK's table and end it."""
        transport = self._sessions().pop(session_id, None)
        if transport is None:
            return
        try:
            await transport.terminate()
        except Exception:
            logger.exception(failure_event, session_id_prefix=session_id[:8])

    def _release_if_ended(self, session_id: str) -> None:
        """After a DELETE: drop the session if it ended. Since mcp 1.30
        the SDK removes an ended session from its table itself; before,
        it left it there, terminated."""
        sessions = self._sessions()
        transport = sessions.get(session_id)
        if transport is not None and not transport.is_terminated:
            return
        sessions.pop(session_id, None)
        self._forget(session_id)

    def _forget(self, session_id: str) -> None:
        self._in_progress.pop(session_id, None)
        self._idle_since.pop(session_id, None)
        if self._owners.pop(session_id, None) is None:
            return
        if self._on_session_end is None:
            return
        try:
            self._on_session_end(session_id)
        except Exception:
            logger.exception(
                "session.end_report.failed",
                session_id_prefix=session_id[:8],
            )

    def _record(self, session_id: str, owner: SessionOwner | None) -> None:
        if owner is None:
            # Unreachable behind the auth middleware. Recording nothing
            # fails closed: every later request on the session gets 404.
            logger.warning(
                "session.owner.unauthenticated",
                session_id_prefix=session_id[:8],
            )
            return
        if session_id in self._owners:
            return  # Already owned; a new session never reuses an id.
        for ended in [sid for sid in self._owners if not self._is_live(sid)]:
            self._forget(ended)
        self._owners[session_id] = owner
        self._idle_since[session_id] = self._clock()
