"""Every MCP request runs with its own caller and session id.

The MCP SDK handles every request of a session inside the task it
started at ``initialize``, so the context variables the HTTP middleware
sets per request hold what they held then:

- ``current_session_id`` is None, because ``initialize`` carries no
  session id;
- ``current_user_id`` is "anonymous" for every bearer client, because
  only the dashboard cookie sets it.

Tool-call audit rows (allowed and denied) and handler log lines had no
session id, and log lines and Sentry events of bearer clients named
"anonymous". (Actions and rate limits already name the bearer through
``current_caller_id``.) (The caller and org that tools read
through ``auth_context_var`` and ``current_org_id`` are captured the same
way; they are right only because ``SessionOwnerGuard`` lets nobody but
the session's opener, on the org it was opened on, use the session.)

``bind_request_identity`` wraps every request handler of an MCP server.
Before the handler runs, it sets both variables and the log context from
the HTTP request that carried this MCP request, which the SDK hands to
the handler as ``request_ctx.request``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.lowlevel.server import Server, request_ctx
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.types import ServerResult
from starlette.requests import Request
from structlog.contextvars import bound_contextvars

from mcpolis.entrypoints.controllers.gateway_controller import (
    current_session_id,
    current_user_id,
)

RequestHandler = Callable[..., Awaitable[ServerResult]]
# Marks a session as having work in progress while the context lasts
# (``SessionOwnerGuard.busy``).
SessionBusy = Callable[[str | None], AbstractContextManager[None]]


def _never_busy(_session_id: str | None) -> AbstractContextManager[None]:
    return nullcontext()


def current_http_request() -> Request | None:
    """The HTTP request carrying the MCP request being handled, or None
    outside a request or off HTTP (e.g. an in-memory transport)."""
    try:
        request = request_ctx.get().request
    except LookupError:
        return None
    return request if isinstance(request, Request) else None


class RequestIdentityHandler:
    """``handler``, run with its own request's caller and session id."""

    def __init__(
        self, handler: RequestHandler, busy: SessionBusy = _never_busy,
    ) -> None:
        self._handler = handler
        self._busy = busy

    async def __call__(self, request: Any) -> ServerResult:
        http_request = current_http_request()
        if http_request is None:
            return await self._handler(request)
        user = http_request.scope.get("user")
        user_id = (
            user.display_name
            if isinstance(user, AuthenticatedUser)
            else current_user_id.get()
        )
        session_id = http_request.headers.get(MCP_SESSION_ID_HEADER)
        user_reset = current_user_id.set(user_id)
        session_reset = current_session_id.set(session_id)
        try:
            with (
                self._busy(session_id),
                bound_contextvars(user_id=user_id, session_id=session_id),
            ):
                return await self._handler(request)
        finally:
            current_session_id.reset(session_reset)
            current_user_id.reset(user_reset)


def bind_request_identity(
    server: Server[Any, Any], busy: SessionBusy = _never_busy,
) -> None:
    """Run every request handler of ``server`` with its own request's
    caller and session id, its session marked ``busy`` meanwhile. Call
    it once the handlers are registered (FastMCP tools all go through
    one ``tools/call`` handler)."""
    for request_type, handler in list(server.request_handlers.items()):
        if not isinstance(handler, RequestIdentityHandler):
            server.request_handlers[request_type] = RequestIdentityHandler(
                handler, busy,
            )
