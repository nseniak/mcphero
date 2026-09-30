"""A stored-token reconnect that started BEFORE the user signed in again
must never destroy the new sign-in.

The fresh sign-in writes the user's new tokens from the OAuth callback,
outside any reconnect. A reconnect that loaded the OLD tokens a moment
earlier acts on the store by key, on whatever row it finds there:

- when its token refresh succeeds, it writes the refreshed OLD tokens over
  the new ones, silently putting the user back on the old sign-in;
- when it fails, its cleanup deletes the sign-in, so the user who has just
  signed in has to sign in again.

The guard must still let through a refresh of the SAME sign-in that
another refresh saved newer tokens of meanwhile: those are the newest
tokens the upstream issued.

REAL token endpoint (a small Starlette app whose answer the test releases,
so the reconnect is provably mid-refresh when the new sign-in lands), REAL
loopback MCP server, REAL file-backed store, REAL reconnect path.

NOTE: no ``from __future__ import annotations`` (see the race harness).
"""
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcpolis.adapters.repositories.connection_store import (
    OAuthToken as StoredToken,
)
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit._loopback_mcp import free_port, wait_for_health
from tests.unit.factories import make_oauth_metadata
from tests.unit._user_session_harness import (
    ALICE,
    GATEWAY_URL,
    UPSTREAM_ID,
    ConnectionGate,
    acquire,
    make_token,
    make_upstream,
    start_upstream,
    stop_upstream,
    wait_until,
)
# After the harness, which loads ``upstream_connection_service``: imported
# first, ``oauth_refresh`` fails on its circular import with it.
from mcpolis.domain.services.oauth_refresh import refresh_token_for_user


class TokenEndpoint:
    """The upstream's OAuth token endpoint. Counts refresh requests and
    holds each answer until ``release`` is set."""

    def __init__(self, answer: dict[str, Any], status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.requests = 0
        self.release = asyncio.Event()


class RotatingTokenEndpoint:
    """An upstream token endpoint that issues new tokens on every
    refresh, as ``drop`` does, and holds the FIRST request until
    ``release_first`` is set. ``issued`` lists the access tokens in the
    order they were issued."""

    def __init__(self) -> None:
        self.requests = 0
        self.issued: list[str] = []
        self.release_first = asyncio.Event()


async def start_token_endpoint(
    endpoint: TokenEndpoint,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    async def token(_request: Request) -> JSONResponse:
        endpoint.requests += 1
        await endpoint.release.wait()
        return JSONResponse(endpoint.answer, status_code=endpoint.status)

    return await serve_token_endpoint(token)


async def start_rotating_token_endpoint(
    endpoint: RotatingTokenEndpoint,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    async def token(_request: Request) -> JSONResponse:
        endpoint.requests += 1
        if endpoint.requests == 1:
            await endpoint.release_first.wait()
        access_token = f"issued-{len(endpoint.issued) + 1}"
        endpoint.issued.append(access_token)
        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"refresh-{access_token}",
        })

    return await serve_token_endpoint(token)


async def serve_token_endpoint(
    token: Callable[[Request], Awaitable[JSONResponse]],
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    port = free_port()
    app = Starlette(routes=[Route("/token", token, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none",
    ))
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    await wait_for_health(f"{base}/", label="token endpoint")
    return server, task, base


async def make_store_with_old_sign_in(
    tmp_path: Path, token_base: str,
) -> FileConnectionStore:
    """Alice's OLD sign-in: an access token that has expired (so the
    reconnect refreshes it first), the app registration and the upstream's
    OAuth endpoints, as a real consent leaves them."""
    store = FileConnectionStore(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, ALICE, UPSTREAM_ID,
        StoredToken(
            access_token="old-token",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
            scopes=[],
        ),
    )
    await store.put_client_info(
        DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=[AnyUrl(f"{GATEWAY_URL}/api/oauth/upstream/callback")],
            token_endpoint_auth_method="client_secret_post",
        ).model_dump(mode="json"),
    )
    await store.put_oauth_metadata(
        DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        make_oauth_metadata(
            issuer=token_base, token_endpoint=f"{token_base}/token",
        ).model_dump(mode="json"),
    )
    return store


async def sign_in_again(store: FileConnectionStore) -> None:
    """What the OAuth callback of a fresh sign-in writes."""
    await store.put_user_token(
        DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("new-token"),
    )


@pytest.mark.asyncio
async def test_a_reconnect_refreshing_the_old_sign_in_never_overwrites_the_new_one(
    tmp_path: Path,
) -> None:
    endpoint = TokenEndpoint({
        "access_token": "old-token-refreshed",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "old-refresh-2",
    })
    token_server, token_task, token_base = await start_token_endpoint(endpoint)
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store_with_old_sign_in(tmp_path, token_base)
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: endpoint.requests >= 1)

        await sign_in_again(store)
        endpoint.release.set()
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )

        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None, "the new sign-in was deleted"
        assert stored.access_token == "new-token", (
            "the old reconnect wrote its refreshed OLD tokens over the new "
            f"sign-in; the store now holds {stored.access_token!r}"
        )
    finally:
        endpoint.release.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
        await stop_upstream(token_server, token_task)


@pytest.mark.asyncio
async def test_a_failing_reconnect_never_deletes_a_sign_in_made_meanwhile(
    tmp_path: Path,
) -> None:
    """The old refresh token is rejected (``invalid_grant``), and the
    reconnect then fails to reach the MCP server (a closed port). Its
    cleanup deletes the sign-in, which by then is the NEW one."""
    endpoint = TokenEndpoint({"error": "invalid_grant"}, status=400)
    token_server, token_task, token_base = await start_token_endpoint(endpoint)
    upstream = make_upstream(f"http://127.0.0.1:{free_port()}/mcp")
    store = await make_store_with_old_sign_in(tmp_path, token_base)
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: endpoint.requests >= 1)

        await sign_in_again(store)
        endpoint.release.set()
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )

        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.access_token == "new-token", (
            "the old reconnect's cleanup deleted the sign-in made meanwhile"
        )
        client_info = await store.get_client_info(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        )
        assert client_info is not None, (
            "the old reconnect's cleanup deleted the app registration the "
            "new sign-in uses"
        )
        assert await store.get_refresh_failures(
            DEFAULT_ORG_ID, UPSTREAM_ID, ALICE,
        ) is None, "the old sign-in's failure was counted against the new one"
        assert await store.get_connection_error(DEFAULT_ORG_ID, UPSTREAM_ID) is None, (
            "the old sign-in's failure put an error on the dashboard"
        )
    finally:
        endpoint.release.set()
        await mgr.stop_all()
        await stop_upstream(token_server, token_task)


@pytest.mark.asyncio
async def test_a_reconnect_refresh_is_saved_after_the_background_refresh_saved_first(
    tmp_path: Path,
) -> None:
    """Production, 2026-09-30 (Sentry MCPOLIS-BACKEND-18): a connection
    refreshed the sign-in it had loaded after the 10-minute background
    refresh had saved newer tokens of the SAME sign-in. The upstream then
    accepted only the connection's tokens, but they were not saved, as if
    the user had signed in again. The next refresh was rejected, and the
    user was signed out."""
    endpoint = RotatingTokenEndpoint()
    token_server, token_task, token_base = await start_rotating_token_endpoint(
        endpoint,
    )
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store_with_old_sign_in(tmp_path, token_base)
    mgr = UpstreamClientManager([upstream])
    try:
        request = asyncio.create_task(acquire(mgr, upstream, store))
        await wait_until(lambda: endpoint.requests >= 1)

        await refresh_token_for_user(
            DEFAULT_ORG_ID, upstream, ALICE, store, GATEWAY_URL,
        )
        endpoint.release_first.set()
        await asyncio.wait_for(
            asyncio.gather(request, return_exceptions=True), timeout=30,
        )

        assert len(endpoint.issued) == 2, endpoint.issued
        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None, "the sign-in was deleted"
        assert stored.access_token == endpoint.issued[-1], (
            "the reconnect's tokens, the newest the upstream issued, were "
            f"not saved; the store holds {stored.access_token!r}"
        )
    finally:
        endpoint.release_first.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
        await stop_upstream(token_server, token_task)
