"""Fake OAuth-demanding MCP upstream for e2e tests.

Listens on http://localhost:9998 and serves three things:

- An MCP server at ``/mcp/`` that 401s without a valid Bearer token,
  and returns ``WWW-Authenticate: Bearer resource_metadata=...`` so
  the MCP SDK's ``OAuthClientProvider`` picks up the discovery URL.
- OAuth 2.1 metadata + endpoints (``/.well-known/...``, ``/authorize``,
  ``/token``) so the gateway can complete a real authorization round
  trip without touching a third-party provider. The ``/authorize``
  endpoint auto-approves immediately — there's no consent UI — so a
  Playwright spec can drive it via a single non-redirect-following
  GET.
- One MCP tool, ``secret_echo``, that returns the supplied message
  alongside the authenticated email. The 2-admin take-over spec
  asserts the email rotates as the slot owner changes.

Pre-seeded ``client_id`` / ``client_secret`` (``e2e-client`` /
``e2e-secret``) — the upstream is configured with these in
``run-e2e-tests.sh`` so the SDK skips Dynamic Client Registration
and the fake provider doesn't need a ``/register`` endpoint.

Run:  python tests/e2e/oauth_test_mcp_server.py
"""
from __future__ import annotations

import os
import secrets
import sys
from contextvars import ContextVar
from pathlib import Path

# When invoked as ``python tests/e2e/oauth_test_mcp_server.py`` the
# ``mcpolis`` package isn't on ``sys.path``. Push backend's src/
# onto the path so ``mcp.server.fastmcp`` (vendored under the
# project's environment) imports cleanly via the conda env.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from urllib.parse import urlencode  # noqa: E402

import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import (  # noqa: E402
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route  # noqa: E402
from starlette.types import ASGIApp, Receive, Scope, Send  # noqa: E402

# Default 9998 so legacy callers (run-e2e-tests.sh single-shard mode)
# keep working; the sharding orchestrator overrides via
# ``MCPOLIS_OAUTH_TEST_PORT`` so each shard gets its own fake.
PORT = int(os.environ.get("MCPOLIS_OAUTH_TEST_PORT", "9998"))
ISSUER = f"http://localhost:{PORT}"
RESOURCE = f"{ISSUER}/mcp"

CLIENT_ID = "e2e-client"
CLIENT_SECRET = "e2e-secret"

# ``code -> email``. ``/authorize`` writes the code; ``/token`` reads
# it. Keys are short-lived (seconds) but we don't bother with TTL —
# the e2e harness restarts the server per run.
_pending_codes: dict[str, str] = {}

# ``access_token -> email``. The MCP-side Bearer middleware looks up
# the email here. ``/token`` writes the row; the Bearer middleware
# only reads it.
_active_tokens: dict[str, str] = {}

# Test knob: a queue of emails the next ``/authorize`` calls will
# use as the eventual token's identity, *if* the request didn't
# carry an ``?email=`` query param. The browser-driven UI take-over
# spec (tests/e2e/17-admin-oauth-takeover-ui.spec.ts) can't inject
# the query param because the frontend opens the popup with
# whatever URL MCPolis hands it, so it ``POST /test/queue-email``s
# the desired identity before clicking Connect.
_email_queue: list[str] = []

# Token-classification: distinguish access vs refresh tokens so
# ``/test/revoke-access-tokens`` can invalidate one and not the
# other (silent-refresh test) vs both
# (re-auth-required test).
_access_token_set: set[str] = set()
_refresh_token_set: set[str] = set()

# Test introspection: count of successful ``/token`` exchanges
# broken out by grant_type. Lets the silent-refresh spec assert
# "a refresh actually happened" rather than just "the call
# eventually succeeded".
_refresh_grant_count: int = 0

# Test knob: TTL (in seconds) attached to the next minted access
# token. Defaults to 1 hour — production-like — but the silent-
# refresh spec drops it to a few seconds and waits the token out
# so the MCP SDK's proactive expiry-based refresh path fires.
_next_access_ttl: int = 3600

# Test knob: counter of refresh-grant calls to fail with a 503
# before serving normally. Lets the transient-keep spec inject a
# server-side blip without revoking tokens — the gateway should
# observe a refresh failure with no ``error_code`` (i.e. a
# transient signature) and §5.1 should *keep* the token row.
_fail_refresh_count: int = 0

# Per-request ``ContextVar`` keeping the authenticated email
# accessible from inside the FastMCP tool body. The Bearer middleware
# sets it before forwarding; the tool reads it back.
_AUTHED_EMAIL: ContextVar[str] = ContextVar("authed_email", default="")


# ─── OAuth 2.1 metadata ──────────────────────────────────────────────


async def protected_resource_metadata(_request: Request) -> JSONResponse:
    """RFC 9728 — points to which authorization servers can mint
    tokens for this resource. The MCP SDK fetches this off the back
    of a 401 with ``WWW-Authenticate: Bearer resource_metadata=...``."""
    return JSONResponse({
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
    })


async def authorization_server_metadata(_request: Request) -> JSONResponse:
    """RFC 8414 — the authorization server's discovery doc. Lists
    the endpoints the SDK will call (``/authorize``, ``/token``)."""
    return JSONResponse({
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "client_secret_basic",
        ],
    })


# ─── Authorize / token endpoints ─────────────────────────────────────


async def authorize(request: Request) -> Response:
    """Fake consent screen. Auto-approves and redirects back to the
    caller's ``redirect_uri`` with a fresh ``code`` + the original
    ``state``. The ``email`` query param is the e2e harness's way of
    saying "issue a token for this user when the code is exchanged".
    """
    qp = request.query_params
    redirect_uri = qp.get("redirect_uri")
    if not redirect_uri:
        return PlainTextResponse("missing redirect_uri", status_code=400)
    state = qp.get("state", "")
    # In real OAuth the IdP would identify the user from its own
    # session. We pick from (in order): explicit ``?email=`` query
    # param (used by the API-driven specs that build the authorize
    # URL themselves), then the queued identity from
    # ``POST /test/queue-email`` (used by the browser-driven spec
    # whose frontend can't add the query param), then the default.
    email = qp.get("email")
    if not email:
        email = _email_queue.pop(0) if _email_queue else "anonymous@e2e.test"

    code = secrets.token_urlsafe(16)
    _pending_codes[code] = email

    params: dict[str, str] = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}{urlencode(params)}",
        status_code=302,
    )


async def queue_email(request: Request) -> Response:
    """Test-only endpoint: queue an email for the next ``/authorize``
    call that doesn't pass ``?email=``. Lets the UI take-over spec
    pre-set "the user about to click Connect is admin@example.com"
    without having to inject query params into the popup URL."""
    form = await request.form()
    email = form.get("email")
    if not isinstance(email, str) or "@" not in email:
        return JSONResponse({"error": "email required"}, status_code=400)
    _email_queue.append(email)
    return JSONResponse({"queued": email, "depth": len(_email_queue)})


async def revoke_access_tokens(request: Request) -> Response:
    """Test-only: invalidate every active access token (refresh
    tokens stay valid). The next MCP request from the gateway
    arrives with a now-rejected access token; the MCP SDK should
    detect the 401, run its refresh-token grant, and retry. The
    silent-refresh spec asserts that ``_refresh_grant_count``
    increments and that the eventual tool response is the normal
    success shape."""
    del request
    revoked = len(_access_token_set)
    _access_token_set.clear()
    return JSONResponse({"revoked_access_tokens": revoked})


async def set_token_ttl(request: Request) -> Response:
    """Test-only: control the ``expires_in`` value the next ``/token``
    response carries. Drop to a few seconds + wait, and the MCP SDK
    will proactively refresh on the next call (see comment on
    ``_next_access_ttl`` for why this is the right test surface
    rather than 401-driven refresh)."""
    global _next_access_ttl
    form = await request.form()
    raw = form.get("seconds")
    if not isinstance(raw, str):
        return JSONResponse({"error": "seconds required"}, status_code=400)
    try:
        seconds = int(raw)
    except ValueError:
        return JSONResponse({"error": "seconds must be int"}, status_code=400)
    if seconds < 1:
        return JSONResponse({"error": "seconds must be >= 1"}, status_code=400)
    _next_access_ttl = seconds
    return JSONResponse({"next_access_ttl": _next_access_ttl})


async def revoke_all_tokens(request: Request) -> Response:
    """Test-only: invalidate every active token, both access and
    refresh. Forces the MCP SDK's refresh attempt to fail too —
    the gateway should surface a clear "needs re-auth" message
    instead of silently swallowing the error."""
    del request
    revoked_access = len(_access_token_set)
    revoked_refresh = len(_refresh_token_set)
    _access_token_set.clear()
    _refresh_token_set.clear()
    return JSONResponse({
        "revoked_access_tokens": revoked_access,
        "revoked_refresh_tokens": revoked_refresh,
    })


async def fail_next_refresh(request: Request) -> Response:
    """Test-only: arm the next ``count`` refresh-grant calls to
    return a 503 with no ``error_code``. This simulates a
    transient upstream blip (server overload, DNS flap) — the
    gateway's §5.1 policy must keep the token row so a subsequent
    retry can succeed without forcing re-auth."""
    global _fail_refresh_count
    form = await request.form()
    raw = form.get("count")
    count = 1
    if isinstance(raw, str):
        try:
            count = int(raw)
        except ValueError:
            return JSONResponse(
                {"error": "count must be int"}, status_code=400,
            )
    if count < 1:
        return JSONResponse(
            {"error": "count must be >= 1"}, status_code=400,
        )
    _fail_refresh_count = count
    return JSONResponse({"fail_next_refresh_count": _fail_refresh_count})


async def reset_state(request: Request) -> Response:
    """Test-only: zero out every mutable bit of fake-provider state.
    Specs that share the same long-lived process call this in their
    beforeEach hooks so a previous spec's TTL knob, queued email,
    or in-flight refresh count can't leak into the next."""
    del request
    global _refresh_grant_count, _next_access_ttl, _fail_refresh_count
    _pending_codes.clear()
    _active_tokens.clear()
    _access_token_set.clear()
    _refresh_token_set.clear()
    _email_queue.clear()
    _refresh_grant_count = 0
    _next_access_ttl = 3600
    _fail_refresh_count = 0
    return JSONResponse({"reset": True})


async def state(request: Request) -> Response:
    """Test-only introspection. The silent-refresh spec reads
    ``refresh_grant_count`` to assert "a refresh actually
    happened" rather than just "the call eventually succeeded."""
    del request
    return JSONResponse({
        "active_access_tokens": len(_access_token_set),
        "active_refresh_tokens": len(_refresh_token_set),
        "refresh_grant_count": _refresh_grant_count,
        "email_queue_depth": len(_email_queue),
        "fail_next_refresh_count": _fail_refresh_count,
    })


async def token(request: Request) -> Response:
    """Standard OAuth 2.1 token endpoint. Accepts both
    ``authorization_code`` (initial exchange) and ``refresh_token``
    (silent renewal). ``client_id`` / ``client_secret`` are checked
    against the pre-seeded values so a wrong-credentials test would
    fail loud."""
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        return JSONResponse(
            {"error": "invalid_client"}, status_code=401,
        )

    global _refresh_grant_count
    if grant_type == "authorization_code":
        code = form.get("code")
        if not isinstance(code, str) or code not in _pending_codes:
            return JSONResponse(
                {"error": "invalid_grant"}, status_code=400,
            )
        email = _pending_codes.pop(code)
    elif grant_type == "refresh_token":
        global _fail_refresh_count
        if _fail_refresh_count > 0:
            # Transient-blip simulation: return a 5xx with no OAuth
            # ``error_code`` so the gateway's refresh classifier
            # treats this as transient (not invalid_grant) and
            # the §5.1 policy keeps the token row.
            _fail_refresh_count -= 1
            return JSONResponse(
                {"error": "service_unavailable"}, status_code=503,
            )
        refresh_token_in = form.get("refresh_token")
        if (
            not isinstance(refresh_token_in, str)
            or refresh_token_in not in _refresh_token_set
        ):
            return JSONResponse(
                {"error": "invalid_grant"}, status_code=400,
            )
        email = _active_tokens[refresh_token_in]
        # Rotate: the old refresh token is now invalid (RFC 6749 §6
        # recommends rotation; the test relies on this to verify a
        # refresh actually happened).
        _refresh_token_set.discard(refresh_token_in)
        _refresh_grant_count += 1
    else:
        return JSONResponse(
            {"error": "unsupported_grant_type"}, status_code=400,
        )

    access_token = secrets.token_urlsafe(24)
    refresh_token_out = secrets.token_urlsafe(24)
    _active_tokens[access_token] = email
    _active_tokens[refresh_token_out] = email
    _access_token_set.add(access_token)
    _refresh_token_set.add(refresh_token_out)

    return JSONResponse({
        "access_token": access_token,
        "refresh_token": refresh_token_out,
        "token_type": "Bearer",
        "expires_in": _next_access_ttl,
        "scope": "openid email",
    })


# ─── MCP server gated by Bearer auth ─────────────────────────────────


def _build_mcp() -> FastMCP:
    # ``stateless_http=True`` + ``json_response=True`` matches the
    # configuration the demo server uses (see
    # ``mcpolis.dev.demo_mcp_server.build_demo_mcp``). It collapses
    # the streamable-HTTP session protocol into one request /one
    # response so the gateway can call ``initialize`` and ``tools/
    # call`` without keeping per-session state on the upstream side.
    # Without this flag the gateway's "Session terminated" arrives
    # before the call ever lands here.
    server = FastMCP(
        name="OAuthTestUpstream",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    @server.tool(
        name="secret_echo",
        description="Echo with the authenticated email appended",
    )
    def secret_echo(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        # The Bearer middleware has already validated and stuffed
        # the email into ``_AUTHED_EMAIL``; reading it here keeps
        # the tool body synchronous.
        return f"{message} | as={_AUTHED_EMAIL.get()}"

    return server


class BearerAuthMiddleware:
    """Pure-ASGI middleware that fails any ``/mcp`` request without
    ``Authorization: Bearer <known_token>``. The 401 response carries
    a ``WWW-Authenticate`` header pointing the MCP SDK at our
    resource-metadata endpoint — that's how MCPolis discovers the
    OAuth flow without prior configuration.

    Implemented as raw ASGI rather than ``BaseHTTPMiddleware``
    because the latter buffers the response body — which breaks
    streamable-HTTP / SSE responses that MCP's session protocol
    relies on (the session's keep-alive frames never reach the
    client and the inner session times out as ``Session terminated``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self._app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth_header = headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            await _send_unauthorized(send)
            return
        token_value = auth_header.split(" ", 1)[1].strip()
        # Strict access-token check (not just "any minted token") so
        # ``/test/revoke-access-tokens`` can invalidate a token mid-
        # session and force the MCP SDK's refresh path. A revoked
        # token still appears in ``_active_tokens`` (we keep the
        # email mapping for log readability) but is removed from
        # ``_access_token_set``.
        if token_value not in _access_token_set:
            await _send_unauthorized(send)
            return
        email = _active_tokens.get(token_value)
        if email is None:
            await _send_unauthorized(send)
            return

        ctx_token = _AUTHED_EMAIL.set(email)
        try:
            await self._app(scope, receive, send)
        finally:
            _AUTHED_EMAIL.reset(ctx_token)


async def _send_unauthorized(send: Send) -> None:
    www_authenticate = (
        f'Bearer realm="mcp", '
        f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
    )
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"www-authenticate", www_authenticate.encode()),
            (b"content-length", b"0"),
        ],
    })
    await send({"type": "http.response.body", "body": b"", "more_body": False})


# ─── App assembly ────────────────────────────────────────────────────


def main() -> None:
    mcp = _build_mcp()
    # ``streamable_http_app()`` returns a Starlette app whose router
    # already has the ``/mcp`` route registered (because we passed
    # ``streamable_http_path="/mcp"`` to FastMCP). Add the OAuth
    # routes to the same router so everything shares one app + one
    # lifespan; no Mount needed (and so no slash-redirect to dodge).
    mcp_app: Starlette = mcp.streamable_http_app()
    extra_routes = [
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=protected_resource_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=authorization_server_metadata,
            methods=["GET"],
        ),
        Route("/authorize", endpoint=authorize, methods=["GET"]),
        Route("/token", endpoint=token, methods=["POST"]),
        Route("/test/queue-email", endpoint=queue_email, methods=["POST"]),
        Route(
            "/test/revoke-access-tokens",
            endpoint=revoke_access_tokens,
            methods=["POST"],
        ),
        Route(
            "/test/revoke-all-tokens",
            endpoint=revoke_all_tokens,
            methods=["POST"],
        ),
        Route(
            "/test/set-token-ttl",
            endpoint=set_token_ttl,
            methods=["POST"],
        ),
        Route(
            "/test/fail-next-refresh",
            endpoint=fail_next_refresh,
            methods=["POST"],
        ),
        Route("/test/reset", endpoint=reset_state, methods=["POST"]),
        Route("/test/state", endpoint=state, methods=["GET"]),
    ]
    mcp_app.router.routes.extend(extra_routes)

    # Wrap the FastMCP-owned Starlette in our pure-ASGI auth
    # middleware so the middleware runs before routing (and so its
    # raw 401 doesn't get post-processed by Starlette's exception
    # handlers).
    app: ASGIApp = BearerAuthMiddleware(mcp_app)

    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT,
        log_level="warning", ws="none",
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
