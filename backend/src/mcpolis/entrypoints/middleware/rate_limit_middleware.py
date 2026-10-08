"""HTTP rate limiting for sign-in endpoints and the dashboard API.

Classifies each request by URL path and asks ``RateLimitService``
whether it may proceed. Refused → HTTP 429 with ``Retry-After``.

Categories (see ``RateLimitService`` for the numbers):

* **sign-in** — keyed by client IP, one bucket per ``SignInGroup``:
  the gateway / Admin MCP OAuth endpoints (``/authorize``, ``/token``,
  ``/register``, ``/revoke``, the Google callback, the consent page)
  under ``/mcp`` and
  ``/admin-mcp``; the dashboard sign-in (``/api/auth/login``,
  ``/api/auth/callback``, the dev-stub picker, the test-mode token
  mint) and the upstream OAuth callback; the public org lookup behind
  invite links; browser error reports. All answer anonymous callers, so
  the IP is the only key there is. A gateway sign-in request then runs
  with its address as the source of the sign-in state it leaves behind
  (``sign_in_requests_from``), so the gateway's per-source caps count
  callers the way this limiter does, except that an IPv6 source spans a
  /48 (``sign_in_source``).
* **dashboard** — every other ``/api/*`` request, keyed by the
  signed-in user (session cookie), or the client IP when anonymous.
  Except the two live-update streams (EventSource): a browser that gets
  a non-200 answer to an EventSource never retries, so a 429 there
  would silently freeze the page until a reload.
* Everything else passes untouched. Gateway and Admin MCP *tool calls*
  are limited inside their MCP handlers, not here: only there is the
  caller and its org known, and an MCP client reads a tool error where
  an HTTP 429 would look like a broken connection.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Literal

import structlog
from mcp.server.auth.routes import (
    AUTHORIZATION_PATH,
    REGISTRATION_PATH,
    REVOCATION_PATH,
    TOKEN_PATH,
)
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcpolis.adapters.auth.dev_stub_oauth_provider import PICKER_PATH, SUBMIT_PATH
from mcpolis.adapters.auth.mcp_gateway_oauth_provider import sign_in_requests_from
from mcpolis.domain.services.rate_limit_service import (
    RateLimitRefusal,
    RateLimitService,
    SignInGroup,
)
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    get_session_payload,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_GATEWAY_PREFIXES = ("/mcp/", "/admin-mcp/")
_GATEWAY_SIGN_IN_SUFFIXES = (
    AUTHORIZATION_PATH,
    TOKEN_PATH,
    REGISTRATION_PATH,
    REVOCATION_PATH,
    "/oauth/google/callback",
    # The approve-this-client page between Google sign-in and the code.
    "/oauth/consent",
)
SIGN_IN_API_PATHS: dict[str, SignInGroup] = {
    "/api/auth/login": SignInGroup.dashboard_sign_in,
    "/api/auth/callback": SignInGroup.dashboard_sign_in,
    f"/api/auth{PICKER_PATH}": SignInGroup.dashboard_sign_in,
    f"/api/auth{SUBMIT_PATH}": SignInGroup.dashboard_sign_in,
    "/api/auth/test-mcp-token": SignInGroup.dashboard_sign_in,
    "/api/oauth/upstream/callback": SignInGroup.dashboard_sign_in,
    "/api/client-errors": SignInGroup.client_errors,
}
_PUBLIC_ORG_LOOKUP = re.compile(r"^/api/orgs/[^/]+/public$")
# EventSource endpoints: one long request each, never refused (see the
# module docstring). They still require a signed-in session.
EVENT_STREAM_PATHS = frozenset({"/api/events"})
_UPSTREAM_LOG_STREAM = re.compile(r"^/api/admin/upstreams/[^/]+/logs/stream$")

Classification = SignInGroup | Literal["dashboard"]


def is_event_stream_path(path: str) -> bool:
    """One of the two EventSource endpoints. A browser never retries an
    EventSource that got a non-200 answer, so nothing refuses them: not
    this limiter, not the shutdown drain (``DrainMiddleware``)."""
    path = path.rstrip("/") or "/"
    return path in EVENT_STREAM_PATHS or bool(_UPSTREAM_LOG_STREAM.match(path))


def classify(path: str) -> Classification | None:
    """Map a URL path to the bucket kind it is charged to, or ``None``."""
    path = path.rstrip("/") or "/"
    group = SIGN_IN_API_PATHS.get(path)
    if group is not None:
        return group
    if _PUBLIC_ORG_LOOKUP.match(path):
        return SignInGroup.org_lookup
    if path.startswith(_GATEWAY_PREFIXES) and path.endswith(
        _GATEWAY_SIGN_IN_SUFFIXES,
    ):
        return SignInGroup.mcp_oauth
    if is_event_stream_path(path):
        return None
    if path.startswith("/api/"):
        return "dashboard"
    return None


def client_ip(scope: Scope, trusted_proxy_hops: int) -> str:
    """The caller's IP, trusting exactly ``trusted_proxy_hops`` proxies.

    Each trusted proxy appends the address it received the request
    from to ``X-Forwarded-For``, so the client is the entry that many
    places from the right. Entries further left were written by the
    client and are ignored — reading the leftmost entry would let any
    caller pick its own IP and dodge the per-IP limit.
    """
    peer = scope.get("client")
    peer_host = peer[0] if peer else "unknown"
    if trusted_proxy_hops <= 0:
        return peer_host
    entries = [
        entry.strip()
        for value in Headers(scope=scope).getlist("x-forwarded-for")
        for entry in value.split(",")
        if entry.strip()
    ]
    if not entries:
        return peer_host
    if len(entries) < trusted_proxy_hops:
        # Shorter than the proxy chain: the request skipped a proxy.
        # The leftmost entry was still appended by one of ours.
        return entries[0]
    return entries[-trusted_proxy_hops]


# The IPv6 network a gateway sign-in source spans (``sign_in_source``).
SIGN_IN_SOURCE_IPV6_PREFIX = 48


def ip_bucket_key(ip: str, *, ipv6_prefix: int = 64) -> str:
    """The per-IP bucket an address is counted in.

    IPv6 is counted per ``ipv6_prefix`` network, /64 by default: a single
    client commonly holds a whole /64 and could otherwise rotate
    addresses to dodge the limit. An IPv4 address written in IPv6 form
    (``::ffff:a.b.c.d``, as dual-stack sockets report IPv4 peers) is
    counted as that IPv4 address: its /64 would hold every IPv4 client
    at once.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return str(
            ipaddress.ip_network(f"{address}/{ipv6_prefix}", strict=False),
        )
    return str(address)


def sign_in_source(ip: str) -> str:
    """The source a gateway sign-in request's leftovers count against
    (``sign_in_requests_from``): its address as ``ip_bucket_key`` keys
    it, an IPv6 one per /48 rather than per /64.

    A source's caps make a flood push out its own pending sign-ins and
    registrations, not a real person's (``MAX_PER_SOURCE`` in the gateway
    sign-in provider). Anyone can get a /48 (a free tunnel-broker
    allocation), which holds 65,536 /64s: one request from each of 2,000
    of them filled the total cap one item per source, and the tie-break
    then pushed out the oldest item of all, a real person's pending
    sign-in. The request limits stay per /64, where a /48 would put
    everyone behind one large network (a company, a campus) in one
    bucket; 100 pending sign-ins per /48 leave such a network room.
    """
    return ip_bucket_key(ip, ipv6_prefix=SIGN_IN_SOURCE_IPV6_PREFIX)


def refusal_for(
    scope: Scope,
    *,
    status_code: int,
    retry_after_seconds: int,
    message: str,
    oauth_error: str,
    api_body: dict[str, object],
) -> Response:
    """The answer to a request refused before any route ran (rate limit,
    shutdown drain), in the shape its caller reads, with ``Retry-After``.

    * Browser navigations (the sign-in redirects) get a plain sentence.
    * MCP OAuth endpoints get an OAuth-shaped error whose ``oauth_error``
      code the MCP SDK knows: on a refused token refresh it then keeps
      its saved sign-in and reports a temporary error. An unknown code,
      or a body that is not OAuth-shaped, makes the TypeScript SDK treat
      it as a server error, drop the refresh and restart the interactive
      sign-in.
    * Everything else gets ``api_body``, the JSON the dashboard's
      ``apiFetch`` shows to the user.
    """
    headers = {"Retry-After": str(retry_after_seconds)}
    if "text/html" in Headers(scope=scope).get("accept", ""):
        return PlainTextResponse(message, status_code=status_code, headers=headers)
    if classify(scope["path"]) == SignInGroup.mcp_oauth:
        return JSONResponse(
            {"error": oauth_error, "error_description": message},
            status_code=status_code,
            headers=headers,
        )
    return JSONResponse(api_body, status_code=status_code, headers=headers)


def refusal_response(refusal: RateLimitRefusal, scope: Scope) -> Response:
    """HTTP 429 for a refused request (see ``refusal_for``): the MCP SDK
    knows ``too_many_requests``, and the dashboard shows ``message``."""
    return refusal_for(
        scope,
        status_code=429,
        retry_after_seconds=refusal.retry_after_seconds,
        message=refusal.message,
        oauth_error="too_many_requests",
        api_body={
            "error": "rate_limited",
            "retry_after": refusal.retry_after_seconds,
            "message": refusal.message,
        },
    )


def is_private_non_loopback(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback


class RateLimitMiddleware:
    """ASGI middleware enforcing the sign-in and dashboard limits."""

    def __init__(
        self,
        app: ASGIApp,
        service: RateLimitService,
        settings: Settings,
    ) -> None:
        self._app = app
        self._service = service
        self._settings = settings
        self._warned_private_ip = False

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self._app(scope, receive, send)
            return
        kind = classify(scope["path"])
        if kind is None:
            await self._app(scope, receive, send)
            return
        ip = client_ip(scope, self._settings.trusted_proxy_hops)
        self._check_proxy_hops(ip)
        if kind == "dashboard":
            refusal = await self._service.admit_dashboard_request(
                user=self._session_email(scope), client_ip=ip_bucket_key(ip),
            )
        else:
            refusal = await self._service.admit_sign_in_request(
                client_ip=ip_bucket_key(ip), group=kind,
            )
        if refusal is not None:
            await refusal_response(refusal, scope)(scope, receive, send)
            return
        if kind == SignInGroup.mcp_oauth:
            # The gateway's sign-in state counts against the address too,
            # an IPv6 one over a wider network than the limit's.
            with sign_in_requests_from(sign_in_source(ip)):
                await self._app(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _check_proxy_hops(self, ip: str) -> None:
        """Raise the alarm, once per process, when the per-IP limits
        key on a private address.

        Behind the production proxies that means
        ``MCPOLIS_TRUSTED_PROXY_HOPS`` is too low: every client would be
        counted as the proxy, one shared bucket for the whole platform
        (a sign-in outage once traffic grows). Loopback is excluded so
        dev and test stacks stay quiet.
        """
        if self._warned_private_ip or not is_private_non_loopback(ip):
            return
        self._warned_private_ip = True
        logger.error(
            "rate_limit.client_ip.private",
            client_ip=ip,
            trusted_proxy_hops=self._settings.trusted_proxy_hops,
            hint=(
                "Per-IP rate limits are keying on a private address. Behind "
                "reverse proxies, MCPOLIS_TRUSTED_PROXY_HOPS is likely too "
                "low and all clients share one bucket. Expected only when "
                "users really connect from a private network."
            ),
        )

    def _session_email(self, scope: Scope) -> str | None:
        cookies = cookie_parser(Headers(scope=scope).get("cookie", ""))
        payload = get_session_payload(self._settings, cookies.get(COOKIE_NAME))
        if payload is None:
            return None
        email = payload.get("email")
        return email if isinstance(email, str) else None
