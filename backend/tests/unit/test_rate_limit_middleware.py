"""Tests for the Phase 2d ``RateLimitMiddleware``.

Mounts a tiny Starlette app, hits it through the middleware, and
asserts the boundary conditions: allowed below the limit, 429 with a
``Retry-After`` header at and above the limit, category-specific
quotas are tracked independently, and unclassified paths are never
blocked.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.entrypoints.middleware.rate_limit_middleware import (
    RateLimitConfig,
    RateLimitMiddleware,
)


def _make_app(config: RateLimitConfig) -> Starlette:
    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/mcp/tool", ok, methods=["POST"]),
            Route("/mcp/token", ok, methods=["POST"]),
            Route("/api/upstreams", ok, methods=["GET"]),
            Route("/health", ok, methods=["GET"]),
        ],
    )
    limiter = InProcessRateLimiter()
    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        limiter=limiter,
        config=config,
    )
    return app


def test_tool_endpoint_blocks_after_threshold() -> None:
    config = RateLimitConfig.from_limits_per_minute(auth=5, tool=3, admin=5)
    app = _make_app(config)
    client = TestClient(app)

    for _ in range(3):
        resp = client.post("/mcp/tool")
        assert resp.status_code == 200

    blocked = client.post("/mcp/tool")
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") is not None
    body = blocked.json()
    assert body["error"] == "rate_limited"
    assert body["category"] == "tool"


def test_auth_and_tool_quotas_are_independent() -> None:
    """A saturated tool quota must not leak into the auth category —
    the middleware keys by ``category:scope`` so the two counters are
    unrelated."""
    config = RateLimitConfig.from_limits_per_minute(auth=2, tool=2, admin=5)
    app = _make_app(config)
    client = TestClient(app)

    # Burn the tool quota.
    for _ in range(2):
        assert client.post("/mcp/tool").status_code == 200
    assert client.post("/mcp/tool").status_code == 429

    # Auth endpoint should still have its own untouched budget.
    for _ in range(2):
        assert client.post("/mcp/token").status_code == 200
    assert client.post("/mcp/token").status_code == 429


def test_dashboard_api_never_blocked() -> None:
    """Dashboard API endpoints are behind cookie auth and should not
    be rate-limited — the real abuse surface is the MCP gateway."""
    config = RateLimitConfig.from_limits_per_minute(auth=1, tool=1, admin=1)
    app = _make_app(config)
    client = TestClient(app)

    for _ in range(20):
        assert client.get("/api/upstreams").status_code == 200


def test_health_endpoint_never_blocked() -> None:
    """``/health`` is outside every category and must not consume
    quota or ever 429 — otherwise the load balancer's health probes
    would take the service out of rotation under load."""
    config = RateLimitConfig.from_limits_per_minute(auth=1, tool=1, admin=1)
    app = _make_app(config)
    client = TestClient(app)

    for _ in range(20):
        resp = client.get("/health")
        assert resp.status_code == 200


def test_retry_after_header_is_positive_integer() -> None:
    config = RateLimitConfig.from_limits_per_minute(auth=5, tool=1, admin=5)
    app = _make_app(config)
    client = TestClient(app)

    assert client.post("/mcp/tool").status_code == 200
    blocked = client.post("/mcp/tool")
    assert blocked.status_code == 429
    retry = blocked.headers["Retry-After"]
    assert retry.isdigit()
    assert int(retry) >= 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
