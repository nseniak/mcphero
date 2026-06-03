"""Application-layer rate limiting middleware.

Categorizes each request by URL path, picks the right (limit, window,
key) triple, and asks the injected ``RateLimiter`` whether the caller
is over quota. Over quota → 429 with ``Retry-After``.

Limit categories
----------------
* **auth** — OAuth / login endpoints under ``/mcp``/``/admin-mcp``
  (``/token``, ``/authorize``, ``/register``, Google callback). Keyed
  by client IP. Brute-force / credential stuffing surface.
* **tool** — MCP tool calls under ``/mcp``. Keyed by org. Business
  logic surface.
* **admin** — Admin MCP + dashboard ``/api/*``. Keyed by org. Config
  changes are low-frequency; a noisy caller here is usually a runaway
  script.
* **none** — health / static / other low-risk endpoints are not
  limited at all.

The category→limit mapping is read once at construction time from the
``Settings`` object, so runtime changes require a process restart (same
model as every other config in the app).
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mcpolis.domain.ports.rate_limiter import RateLimiter
from mcpolis.entrypoints.controllers.gateway_controller import current_org_id

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _CategoryConfig:
    limit: int
    window_seconds: float


@dataclass(frozen=True)
class RateLimitConfig:
    auth: _CategoryConfig
    tool: _CategoryConfig
    admin: _CategoryConfig

    @classmethod
    def from_limits_per_minute(
        cls, *, auth: int, tool: int, admin: int,
    ) -> RateLimitConfig:
        return cls(
            auth=_CategoryConfig(limit=auth, window_seconds=60.0),
            tool=_CategoryConfig(limit=tool, window_seconds=60.0),
            admin=_CategoryConfig(limit=admin, window_seconds=60.0),
        )


_AUTH_PATH_FRAGMENTS = (
    "/token",
    "/authorize",
    "/register",
    "/callback",
    "/google-callback",
)


def _classify(path: str) -> str | None:
    """Map a URL path to a category, or ``None`` for "don't limit"."""
    if path.startswith("/health") or path == "/health":
        return None
    if path.startswith("/assets/"):
        return None
    if path.startswith("/.well-known/"):
        return None
    # Dashboard API endpoints (/api/*) are behind cookie auth and
    # only accessible to signed-in users. Don't rate-limit them —
    # the real abuse surface is the MCP gateway, not a human
    # clicking a dashboard.
    if path.startswith("/api/"):
        return None
    # MCP OAuth endpoints: strict per-IP limit to deter brute-force.
    for frag in _AUTH_PATH_FRAGMENTS:
        if path.endswith(frag) or frag + "/" in path:
            return "auth"
    if path.startswith("/mcp"):
        return "tool"
    if path.startswith("/admin-mcp"):
        return "admin"
    return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # First hop is the real client in a properly configured LB.
        return forwarded.split(",")[0].strip()
    client = request.client
    if client is None:
        return "unknown"
    return client.host


def _key_for(category: str, request: Request) -> str:
    if category == "auth":
        return f"auth:{_client_ip(request)}"
    # Tool + admin are org-scoped. ContextVar is set by the main
    # user_identity_middleware, but that middleware runs *after* us
    # (we're added last so we run first); fall back to the default
    # org id for requests that reach the middleware before the
    # identity layer has a chance to run (e.g. health-adjacent paths
    # that still fall through to a limited category).
    try:
        org_id = current_org_id.get()
    except LookupError:
        org_id = "default"
    return f"{category}:{org_id}"


class RateLimitMiddleware:
    """ASGI middleware that enforces per-category rate limits."""

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        config: RateLimitConfig,
    ) -> None:
        self._app = app
        self._limiter = limiter
        self._config = config

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        category = _classify(request.url.path)
        if category is None:
            await self._app(scope, receive, send)
            return

        cat_cfg = getattr(self._config, category)
        key = _key_for(category, request)
        result = await self._limiter.check(
            key,
            limit=cat_cfg.limit,
            window_seconds=cat_cfg.window_seconds,
        )
        if not result.allowed:
            retry_after = max(1, int(round(result.retry_after or 1.0)))
            logger.info(
                "rate_limit.exceeded",
                category=category,
                key=key,
                retry_after_seconds=retry_after,
            )
            response: Response = JSONResponse(
                {
                    "error": "rate_limited",
                    "category": category,
                    "retry_after": retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)
