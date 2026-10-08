"""Refuse a request whose body is larger than a limit.

Wraps one ASGI app (a route). A declared ``Content-Length`` over the
limit is refused before anything is read; a body sent without one (or
longer than it said) is refused as soon as the bytes received pass the
limit, before the app has parsed it. A query string over the limit is
refused too: a form sent with GET carries its fields there. Either way
the caller gets the ``refusal`` response, HTTP 413.
"""
from __future__ import annotations

from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimit:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        refusal: Callable[[], Response],
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._refusal = refusal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if (
            declared.isdigit() and int(declared) > self._max_bytes
        ) or len(scope.get("query_string", b"")) > self._max_bytes:
            await self._refusal()(scope, receive, send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def watching_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, counting_receive, watching_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._refusal()(scope, receive, send)
