"""Every request that changes something on the dashboard or the operator
pages runs to its end once started, whatever cancels it.

A dashboard write that is not one of the shared admin actions (editing an
MCP, its Variables and Sandbox files, an operator's plan change or
sign-out, a member's sign-out of an MCP) used to stop wherever a cancel
caught it: a member's sign-out could delete the saved sign-in and leave
the live session serving them. Two kinds of cancel reach a request: an
anyio cancel scope (Starlette's ``BaseHTTPMiddleware``, when anything
above the route raises) and a native ``Task.cancel()`` (uvicorn, for the
requests still running when its graceful-shutdown time is up).

One mechanism for all of them, so a new route is covered without anyone
remembering to: this middleware runs the request in a task of its own
(``finish_despite_cancels``) when it changes state, i.e. a non-GET request
under ``/api/``. The shutdown waits for these tasks before it closes the
stores (``drain_every_set``).

Not covered, on purpose:

- GET (and HEAD, OPTIONS) requests. They read, or hold a stream open
  until the client leaves (the dashboard's live updates, an MCP's log
  stream), which would then never end. The two GETs that change
  something are sign-in steps a person retries: the member sign-in
  (``/api/auth/connect/{id}``) waits up to 30 s for the browser's
  redirect URL, which nobody reads once the request is gone, while its
  token exchange already runs in a background task of its own; and the
  sign-in callbacks, which hand over a code.
- Everything outside ``/api/``. The MCP endpoints have their own: the
  Admin MCP's ``tools/call`` wrapper and the gateway's shielded audit
  write.

It must stay the outermost middleware. Inside a ``BaseHTTPMiddleware``
layer, a cancelled request would leave this task sending its response
into that layer's stream, which nobody reads any more: the request would
then never end.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from mcpolis.domain.services.cancel_shield import finish_despite_cancels

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def changes_state(scope: Scope) -> bool:
    """A dashboard or operator request that changes something."""
    return (
        scope["type"] == "http"
        and scope["method"] not in _READ_METHODS
        and str(scope["path"]).startswith("/api/")
    )


class RunToCompletionMiddleware:
    """Runs every state-changing ``/api/`` request to its end (see the
    module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not changes_state(scope):
            await self._app(scope, receive, send)
            return

        async def serve() -> None:
            await self._app(scope, receive, send)

        # Named after the request, so a shutdown that has to cut it says
        # which one it was.
        await finish_despite_cancels(
            serve(), name=f"{scope['method']} {scope['path']}",
        )
