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

import httpx
import pytest
import structlog
import uvicorn
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken as SdkToken
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
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
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    _build_oauth_provider,
    _noop_callback,
    _noop_redirect,
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
    ``release_first`` is set; with ``reject_first`` it then answers that
    request ``invalid_grant``. ``issued`` lists the access tokens in the
    order they were issued."""

    def __init__(self, *, reject_first: bool = False) -> None:
        self.requests = 0
        self.issued: list[str] = []
        self.release_first = asyncio.Event()
        self.reject_first = reject_first


class StrictRotatingUpstream:
    """An upstream whose token endpoint accepts each refresh token once
    (reuse is ``invalid_grant``, as with refresh-token rotation), issues a
    1-second access token first and 1-hour ones after, and whose
    ``/resource`` records the bearer each request carried."""

    def __init__(self, first_refresh_token: str) -> None:
        self.refreshes = 0
        self.issued: list[str] = []
        self.valid_refresh_tokens = {first_refresh_token}
        self.bearers: list[str | None] = []


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
            if endpoint.reject_first:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access_token = f"issued-{len(endpoint.issued) + 1}"
        endpoint.issued.append(access_token)
        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"refresh-{access_token}",
        })

    return await serve_token_endpoint(token)


async def start_strict_rotating_upstream(
    upstream: StrictRotatingUpstream,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    async def token(request: Request) -> JSONResponse:
        upstream.refreshes += 1
        form = await request.form()
        refresh_token = form.get("refresh_token")
        if refresh_token not in upstream.valid_refresh_tokens:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        upstream.valid_refresh_tokens.discard(str(refresh_token))
        access_token = f"issued-{len(upstream.issued) + 1}"
        upstream.issued.append(access_token)
        upstream.valid_refresh_tokens.add(f"refresh-{access_token}")
        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 1 if len(upstream.issued) == 1 else 3600,
            "refresh_token": f"refresh-{access_token}",
        })

    async def resource(request: Request) -> JSONResponse:
        upstream.bearers.append(request.headers.get("authorization"))
        return JSONResponse({})

    return await serve_token_endpoint(
        token, extra=[Route("/resource", resource, methods=["POST"])],
    )


async def serve_token_endpoint(
    token: Callable[[Request], Awaitable[JSONResponse]],
    *,
    extra: list[Route] | None = None,
) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    port = free_port()
    app = Starlette(
        routes=[Route("/token", token, methods=["POST"]), *(extra or [])],
    )
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


@pytest.mark.asyncio
async def test_a_connection_uses_tokens_another_refresh_saved_instead_of_refreshing_its_old_copy(
    tmp_path: Path,
) -> None:
    """A connection keeps its own copy of the tokens. When the background
    refresh renews the sign-in, that copy's refresh token is used up. The
    connection must pick up the renewed tokens when its copy expires:
    refreshing its old copy is rejected by an upstream that rotates
    refresh tokens (and, with reuse detection, revokes the sign-in)."""
    upstream_server = StrictRotatingUpstream(first_refresh_token="old-refresh")
    server, server_task, base = await start_strict_rotating_upstream(
        upstream_server,
    )
    upstream = make_upstream(f"{base}/resource")
    store = await make_store_with_old_sign_in(tmp_path, base)
    connection = await _build_oauth_provider(
        upstream,
        McpTokenStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, ALICE),
        _noop_redirect, _noop_callback, GATEWAY_URL,
    )
    try:
        async with httpx.AsyncClient(auth=connection) as client:
            await client.post(f"{base}/resource", json={})
            assert upstream_server.bearers == ["Bearer issued-1"]

            await refresh_token_for_user(
                DEFAULT_ORG_ID, upstream, ALICE, store, GATEWAY_URL,
            )
            await asyncio.sleep(1.2)  # the connection's own copy expires
            await client.post(f"{base}/resource", json={})

        assert upstream_server.bearers[-1] == "Bearer issued-2", (
            "the connection did not use the tokens the background refresh "
            f"saved; bearers seen: {upstream_server.bearers}"
        )
        assert upstream_server.refreshes == 2, (
            "the connection refreshed its old copy "
            f"({upstream_server.refreshes} refresh requests)"
        )
    finally:
        await stop_upstream(server, server_task)


class CountingStorage(McpTokenStorage):
    """Counts how often the sign-in library loads the stored tokens."""

    loads = 0

    async def get_tokens(self) -> SdkToken | None:
        self.loads += 1
        return await super().get_tokens()


@pytest.mark.asyncio
async def test_a_connection_does_not_reload_the_sign_in_while_its_copy_is_valid(
    tmp_path: Path,
) -> None:
    """The reload before a refresh must not become a store read on every
    request: while the copy is valid, the connection keeps using it."""
    store = FileConnectionStore(tmp_path)
    await store.put_user_token(
        DEFAULT_ORG_ID, ALICE, UPSTREAM_ID, make_token("tok"),
    )
    storage = CountingStorage(store, DEFAULT_ORG_ID, UPSTREAM_ID, ALICE)
    connection = await _build_oauth_provider(
        make_upstream("http://upstream.test/mcp"), storage,
        _noop_redirect, _noop_callback, GATEWAY_URL,
    )
    bearers: list[str | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bearers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        auth=connection, transport=httpx.MockTransport(answer),
    ) as client:
        for _ in range(5):
            await client.post("http://upstream.test/mcp", json={})

    assert bearers == ["Bearer tok"] * 5
    assert storage.loads == 1, (
        f"the stored sign-in was loaded {storage.loads} times for 5 requests"
    )


@pytest.mark.asyncio
async def test_a_background_refresh_that_saves_its_own_tokens_logs_success(
    tmp_path: Path,
) -> None:
    endpoint = RotatingTokenEndpoint()
    endpoint.release_first.set()
    token_server, token_task, token_base = await start_rotating_token_endpoint(
        endpoint,
    )
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store_with_old_sign_in(tmp_path, token_base)
    try:
        with structlog.testing.capture_logs() as logs:
            await refresh_token_for_user(
                DEFAULT_ORG_ID, upstream, ALICE, store, GATEWAY_URL,
            )

        events = [e["event"] for e in logs]
        assert "oauth.token.refresh.success" in events, events
        assert "oauth.token.refresh.refreshed_elsewhere" not in events, events
    finally:
        await stop_upstream(server, server_task)
        await stop_upstream(token_server, token_task)


@pytest.mark.asyncio
async def test_a_background_refresh_beaten_by_another_refresh_does_not_log_success(
    tmp_path: Path,
) -> None:
    """The background refresh's own request is rejected because a
    reconnect refreshed the same sign-in first. The sign-in is fine, but
    the log must not credit the background refresh with the success."""
    endpoint = RotatingTokenEndpoint(reject_first=True)
    token_server, token_task, token_base = await start_rotating_token_endpoint(
        endpoint,
    )
    server, server_task, url = await start_upstream(ConnectionGate())
    upstream = make_upstream(url)
    store = await make_store_with_old_sign_in(tmp_path, token_base)
    mgr = UpstreamClientManager([upstream])
    try:
        background = asyncio.create_task(refresh_token_for_user(
            DEFAULT_ORG_ID, upstream, ALICE, store, GATEWAY_URL,
        ))
        await wait_until(lambda: endpoint.requests >= 1)
        await asyncio.wait_for(acquire(mgr, upstream, store), timeout=30)

        with structlog.testing.capture_logs() as logs:
            endpoint.release_first.set()
            await asyncio.wait_for(background, timeout=30)

        events = [e["event"] for e in logs]
        assert "oauth.token.refresh.refreshed_elsewhere" in events, events
        assert "oauth.token.refresh.success" not in events, events
        stored = await store.get_user_token(DEFAULT_ORG_ID, ALICE, UPSTREAM_ID)
        assert stored is not None and stored.access_token == "issued-1"
    finally:
        endpoint.release_first.set()
        await mgr.stop_all()
        await stop_upstream(server, server_task)
        await stop_upstream(token_server, token_task)
