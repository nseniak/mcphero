"""A fake OAuth-protected MCP upstream on loopback, for sign-ins driven
end to end: Connect, the sign-in link, the callback handing the code
over, the code exchange, and what is saved afterwards.

It publishes RFC 9728 protected-resource metadata and RFC 8414
authorization-server metadata, registers clients (RFC 7591), issues
``ISSUED_ACCESS_TOKEN`` / ``ISSUED_REFRESH_TOKEN`` at its token endpoint
(held until ``release_token`` is set, with ``hold_token_exchange``), and
answers 401 on its MCP endpoint to any request without the issued
bearer.

Its events are ``threading.Event``: the server can run in a thread of its
own (``serve_in_thread``) for an app driven by a ``TestClient``, whose
requests each run in an event loop of their own.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from tests.unit._loopback_mcp import free_port, wait_for_health

ISSUED_ACCESS_TOKEN = "fresh-at"
ISSUED_REFRESH_TOKEN = "fresh-rt"
# How long a held token exchange waits for ``release_token`` at most, so a
# test that never releases it cannot hang the server's shutdown.
_HOLD_LIMIT_SECONDS = 30.0


class FakeOAuthUpstream:
    def __init__(self, *, hold_token_exchange: bool = False) -> None:
        self.base = ""
        self.hold_token_exchange = hold_token_exchange
        self.token_requested = threading.Event()
        self.release_token = threading.Event()
        self.registrations = 0

    def app(self) -> Starlette:
        async def resource_metadata(_request: Request) -> Response:
            return JSONResponse({
                "resource": f"{self.base}/mcp",
                "authorization_servers": [self.base],
            })

        async def server_metadata(_request: Request) -> Response:
            return JSONResponse({
                "issuer": self.base,
                "authorization_endpoint": f"{self.base}/authorize",
                "token_endpoint": f"{self.base}/token",
                "registration_endpoint": f"{self.base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
            })

        async def register(request: Request) -> Response:
            self.registrations += 1
            metadata = await request.json()
            return JSONResponse({
                **metadata,
                "client_id": f"registered-{self.registrations}",
                "client_secret": "registered-secret",
            }, status_code=201)

        async def token(_request: Request) -> Response:
            self.token_requested.set()
            if self.hold_token_exchange:
                await anyio.to_thread.run_sync(
                    self.release_token.wait, _HOLD_LIMIT_SECONDS,
                )
            return JSONResponse({
                "access_token": ISSUED_ACCESS_TOKEN,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": ISSUED_REFRESH_TOKEN,
            })

        async def mcp(request: Request) -> Response:
            if request.headers.get("authorization") == f"Bearer {ISSUED_ACCESS_TOKEN}":
                return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})
            return Response(
                json.dumps({"error": "invalid_token"}),
                status_code=401,
                headers={
                    "content-type": "application/json",
                    "www-authenticate": (
                        f'Bearer resource_metadata="{self.base}'
                        '/.well-known/oauth-protected-resource"'
                    ),
                },
            )

        async def health(_request: Request) -> Response:
            return Response("ok")

        return Starlette(routes=[
            Route("/", health),
            Route("/.well-known/oauth-protected-resource", resource_metadata),
            Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
            Route("/.well-known/oauth-authorization-server", server_metadata),
            Route("/register", register, methods=["POST"]),
            Route("/token", token, methods=["POST"]),
            Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
        ])


def make_oauth_protected_upstream(
    base: str,
    *,
    upstream_id: str = "guarded",
    client_id: str | None = "c-1",
) -> UpstreamDefinition:
    """A per-user OAuth upstream at ``base``; with ``client_id`` its app
    is registered in advance, without one the sign-in registers it."""
    return UpstreamDefinition(
        id=upstream_id,
        display_name="Guarded",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=f"{base}/mcp"),
        auth=UpstreamAuthConfig(
            mode=AuthMode.per_user_oauth,
            client_id=client_id,
            client_secret="s-1" if client_id is not None else None,
        ),
    )


async def start_fake_oauth_upstream(
    fake: FakeOAuthUpstream,
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Serve ``fake`` in the running event loop; stop it with
    ``tests.unit._user_session_harness.stop_upstream``."""
    port = free_port()
    fake.base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        fake.app(), host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    await wait_for_health(f"{fake.base}/", label="fake oauth upstream")
    return server, task


@contextmanager
def serve_in_thread(fake: FakeOAuthUpstream) -> Iterator[str]:
    """Serve ``fake`` from a thread of its own while the block runs;
    yields its base URL."""
    port = free_port()
    fake.base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        fake.app(), host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise AssertionError("the fake oauth upstream never started")
        time.sleep(0.01)
    try:
        yield fake.base
    finally:
        fake.release_token.set()
        server.should_exit = True
        thread.join(timeout=10)
