"""HTTP rate limiting for sign-in endpoints and the dashboard API.

Two layers: a tiny Starlette app behind ``RateLimitMiddleware`` for the
boundary conditions, and the real ``create_app`` for the wiring (the
middleware is installed, sees the real routes, and the paths it lists
as sign-in endpoints still exist).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Scope

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.adapters.rate_limiter_redis import RedisRateLimiter
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports.rate_limiter import RateLimiter
from mcpolis.domain.services.rate_limit_service import (
    RateLimitService,
    RequestRateLimits,
    SignInGroup,
)
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from mcpolis.entrypoints.middleware.rate_limit_middleware import (
    EVENT_STREAM_PATHS,
    SIGN_IN_API_PATHS,
    Classification,
    RateLimitMiddleware,
    classify,
    client_ip,
    ip_bucket_key,
    sign_in_source,
)
from mcpolis.entrypoints.routes.dashboard_auth import (
    COOKIE_NAME,
    build_session_cookie,
)
from tests.unit.redis_fixture import require_redis

SESSION_SECRET = "rate-limit-test-secret-0123456789"


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        session_secret=SESSION_SECRET,
        **overrides,
    )


def make_app(
    *,
    limits: RequestRateLimits,
    settings: Settings | None = None,
    limiter: RateLimiter | None = None,
) -> TestClient:
    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def free_plan(_org_id: str) -> PlanName:
        return PlanName.free

    app = Starlette(
        routes=[
            Route("/api/auth/login", ok, methods=["GET"]),
            Route("/api/client-errors", ok, methods=["POST"]),
            Route("/api/events", ok, methods=["GET"]),
            Route("/api/orgs/{slug}/public", ok, methods=["GET"]),
            Route("/api/upstreams", ok, methods=["GET", "OPTIONS"]),
            Route("/mcp/token", ok, methods=["POST"]),
            Route("/mcp/", ok, methods=["POST"]),
            Route("/health", ok, methods=["GET"]),
        ],
    )
    app.add_middleware(
        RateLimitMiddleware,  # type: ignore[arg-type]
        service=RateLimitService(
            limiter or InProcessRateLimiter(), limits, plan_for_org=free_plan,
        ),
        settings=settings or make_settings(),
    )
    return TestClient(app)


def make_scope(
    *, peer: str = "172.20.0.6", forwarded_for: list[str] | None = None,
) -> Scope:
    headers = [
        (b"x-forwarded-for", value.encode()) for value in forwarded_for or []
    ]
    return {"type": "http", "client": (peer, 51234), "headers": headers}


def session_cookie(email: str) -> dict[str, str]:
    value = build_session_cookie(make_settings(), email, "acme")
    return {COOKIE_NAME: value}


# ── Which surface a path is charged to ────────────────────────────────


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/mcp/token", SignInGroup.mcp_oauth),
        ("/mcp/acme/token", SignInGroup.mcp_oauth),
        ("/mcp/authorize", SignInGroup.mcp_oauth),
        ("/mcp/register", SignInGroup.mcp_oauth),
        ("/mcp/revoke", SignInGroup.mcp_oauth),
        ("/mcp/oauth/google/callback", SignInGroup.mcp_oauth),
        ("/mcp/oauth/consent", SignInGroup.mcp_oauth),
        ("/admin-mcp/oauth/consent", SignInGroup.mcp_oauth),
        ("/admin-mcp/acme/token", SignInGroup.mcp_oauth),
        ("/admin-mcp/system/authorize", SignInGroup.mcp_oauth),
        ("/api/auth/login", SignInGroup.dashboard_sign_in),
        ("/api/auth/callback", SignInGroup.dashboard_sign_in),
        ("/api/auth/dev-stub/picker", SignInGroup.dashboard_sign_in),
        ("/api/auth/dev-stub/submit", SignInGroup.dashboard_sign_in),
        ("/api/auth/test-mcp-token", SignInGroup.dashboard_sign_in),
        ("/api/oauth/upstream/callback", SignInGroup.dashboard_sign_in),
        ("/api/client-errors", SignInGroup.client_errors),
        ("/api/orgs/acme/public", SignInGroup.org_lookup),
        ("/api/orgs/acme/public/", SignInGroup.org_lookup),
        ("/api/auth/me", "dashboard"),
        ("/api/admin/upstreams", "dashboard"),
        ("/api/admin/service-tokens", "dashboard"),
        ("/api/orgs/acme/info", "dashboard"),
        # Live-update streams: a 429 would freeze them for good.
        ("/api/events", None),
        ("/api/admin/upstreams/github/logs/stream", None),
        ("/api/admin/upstreams/github/logs", "dashboard"),
        # Tool calls are limited inside the MCP handlers, not here.
        ("/mcp/", None),
        ("/mcp/acme", None),
        ("/admin-mcp/acme/", None),
        ("/mcp/.well-known/oauth-authorization-server", None),
        ("/.well-known/oauth-protected-resource/mcp/acme", None),
        ("/health", None),
        ("/assets/index.js", None),
        ("/app/acme/upstreams", None),
    ],
)
def test_classify_charges_each_path_to_its_bucket_kind(
    path: str, kind: Classification | None,
) -> None:
    assert classify(path) == kind


# ── Client IP behind proxies ──────────────────────────────────────────


def test_client_ip_without_proxies_is_the_tcp_peer() -> None:
    scope = make_scope(peer="203.0.113.7", forwarded_for=["198.51.100.1"])

    assert client_ip(scope, trusted_proxy_hops=0) == "203.0.113.7"


def test_client_ip_behind_caddy_and_nginx_is_the_entry_caddy_wrote() -> None:
    # Caddy writes the client, nginx appends Caddy's address.
    scope = make_scope(forwarded_for=["203.0.113.7, 172.18.0.2"])

    assert client_ip(scope, trusted_proxy_hops=2) == "203.0.113.7"


def test_client_ip_ignores_entries_the_client_wrote_itself() -> None:
    """A caller can prepend anything to X-Forwarded-For; reading the
    leftmost entry would let it choose its own rate-limit bucket."""
    scope = make_scope(
        forwarded_for=["1.2.3.4, 5.6.7.8, 203.0.113.7, 172.18.0.2"],
    )

    assert client_ip(scope, trusted_proxy_hops=2) == "203.0.113.7"


def test_client_ip_reads_repeated_forwarded_headers_in_order() -> None:
    scope = make_scope(forwarded_for=["203.0.113.7", "172.18.0.2"])

    assert client_ip(scope, trusted_proxy_hops=2) == "203.0.113.7"


def test_client_ip_with_a_shorter_chain_uses_the_leftmost_entry() -> None:
    scope = make_scope(forwarded_for=["172.18.0.2"])

    assert client_ip(scope, trusted_proxy_hops=2) == "172.18.0.2"


def test_client_ip_without_the_header_falls_back_to_the_peer() -> None:
    scope = make_scope(peer="172.20.0.6")

    assert client_ip(scope, trusted_proxy_hops=2) == "172.20.0.6"


# ── What a refused caller sees ────────────────────────────────────────


def test_sign_in_is_refused_with_429_json_and_retry_after() -> None:
    client = make_app(limits=RequestRateLimits(sign_in_per_min=2))
    for _ in range(2):
        assert client.get("/api/auth/login").status_code == 200

    refused = client.get("/api/auth/login")

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"
    assert refused.json() == {
        "error": "rate_limited",
        "retry_after": 60,
        "message": (
            "Too many sign-in requests from your network. "
            "Try again in 60 seconds."
        ),
    }


def test_error_reports_cannot_close_token_refresh_on_the_same_ip() -> None:
    client = make_app(limits=RequestRateLimits(sign_in_per_min=2))
    for _ in range(5):
        client.post("/api/client-errors")

    assert client.post("/mcp/token").status_code == 200
    assert client.get("/api/auth/login").status_code == 200
    assert client.post("/api/client-errors").status_code == 429


def test_refused_mcp_token_request_speaks_oauth() -> None:
    """``too_many_requests`` is a code the MCP SDK knows: on a refused
    token refresh it keeps the saved sign-in and reports a temporary
    error, instead of restarting the interactive sign-in."""
    client = make_app(limits=RequestRateLimits(sign_in_per_min=1))
    client.post("/mcp/token")

    refused = client.post("/mcp/token")

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"
    assert refused.json() == {
        "error": "too_many_requests",
        "error_description": (
            "Too many sign-in requests from your network. Try again in 60 seconds."
        ),
    }


def test_browser_navigation_gets_a_plain_sentence() -> None:
    client = make_app(limits=RequestRateLimits(sign_in_per_min=1))
    html = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    client.get("/api/auth/login", headers=html)

    refused = client.get("/api/auth/login", headers=html)

    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("text/plain")
    assert refused.text == (
        "Too many sign-in requests from your network. Try again in 60 seconds."
    )


def test_dashboard_is_limited_per_signed_in_user() -> None:
    client = make_app(limits=RequestRateLimits(dashboard_per_min=2))
    alice = session_cookie("alice@example.com")
    bob = session_cookie("bob@example.com")
    for _ in range(2):
        client.cookies = alice
        assert client.get("/api/upstreams").status_code == 200

    client.cookies = alice
    refused = client.get("/api/upstreams")
    client.cookies = bob
    teammate = client.get("/api/upstreams")

    assert refused.status_code == 429
    assert refused.json()["error"] == "rate_limited"
    assert teammate.status_code == 200


def test_dashboard_without_a_valid_session_is_limited_per_ip() -> None:
    client = make_app(limits=RequestRateLimits(dashboard_per_min=1))
    client.cookies = {COOKIE_NAME: "forged-cookie"}
    assert client.get("/api/upstreams").status_code == 200

    # A forged cookie is not a user: it lands in the IP bucket.
    client.cookies = {}
    assert client.get("/api/upstreams").status_code == 429


def test_forwarded_client_ip_picks_the_bucket_behind_trusted_proxies() -> None:
    client = make_app(
        limits=RequestRateLimits(sign_in_per_min=1),
        settings=make_settings(trusted_proxy_hops=2),
    )

    def sign_in_from(forwarded_for: str) -> int:
        return client.get(
            "/api/auth/login", headers={"X-Forwarded-For": forwarded_for},
        ).status_code

    assert sign_in_from("203.0.113.7, 172.18.0.2") == 200
    assert sign_in_from("198.51.100.9, 172.18.0.2") == 200
    # Same real client, new spoofed prefix: still the same bucket.
    assert sign_in_from("10.9.9.9, 203.0.113.7, 172.18.0.2") == 429


def test_gateway_mcp_endpoint_and_health_are_not_limited_here() -> None:
    client = make_app(
        limits=RequestRateLimits(sign_in_per_min=1, dashboard_per_min=1),
    )

    statuses = {client.post("/mcp/").status_code for _ in range(5)}
    statuses |= {client.get("/health").status_code for _ in range(5)}

    assert statuses == {200}


def test_live_update_stream_is_never_refused() -> None:
    client = make_app(limits=RequestRateLimits(dashboard_per_min=1))
    client.cookies = session_cookie("alice@example.com")
    assert client.get("/api/upstreams").status_code == 200
    assert client.get("/api/upstreams").status_code == 429

    assert client.get("/api/events").status_code == 200


def test_ipv6_clients_are_counted_per_64_network() -> None:
    client = make_app(
        limits=RequestRateLimits(sign_in_per_min=1),
        settings=make_settings(trusted_proxy_hops=1),
    )

    def sign_in_from(ip: str) -> int:
        return client.get("/api/auth/login", headers={"X-Forwarded-For": ip}).status_code

    assert sign_in_from("2001:db8:0:1::1") == 200
    assert sign_in_from("2001:db8:0:1::ffff") == 429  # same /64
    assert sign_in_from("2001:db8:0:2::1") == 200  # next /64


@pytest.mark.parametrize(
    ("ip", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:0:1:aaaa:bbbb:cccc:dddd", "2001:db8:0:1::/64"),
        # IPv4 in IPv6 form: its /64 would hold every IPv4 client.
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
        ("unknown", "unknown"),
    ],
)
def test_ip_bucket_key(ip: str, key: str) -> None:
    assert ip_bucket_key(ip) == key


@pytest.mark.parametrize(
    ("ip", "source"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # One /48 holds 65,536 /64s: a gateway sign-in source spans it.
        ("2001:db8:1234:5678:aaaa:bbbb:cccc:dddd", "2001:db8:1234::/48"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
    ],
)
def test_sign_in_source(ip: str, source: str) -> None:
    assert sign_in_source(ip) == source


def test_private_client_ip_raises_one_alarm_per_process() -> None:
    """Behind the production proxies a private client IP means the hop
    count is too low: every client would share one bucket."""
    client = make_app(
        limits=RequestRateLimits(),
        settings=make_settings(trusted_proxy_hops=1),
    )
    behind_caddy = {"X-Forwarded-For": "203.0.113.7, 172.18.0.2"}

    with structlog.testing.capture_logs() as logs:
        client.get("/api/auth/login", headers=behind_caddy)
        client.get("/api/auth/login", headers=behind_caddy)

    alarms = [line for line in logs if line["event"] == "rate_limit.client_ip.private"]
    assert len(alarms) == 1
    assert alarms[0]["log_level"] == "error"
    assert alarms[0]["client_ip"] == "172.18.0.2"


# 8.8.8.8, not a documentation range: Python counts 203.0.113.0/24 as private.
@pytest.mark.parametrize("forwarded_for", ["8.8.8.8", "127.0.0.1"])
def test_public_or_loopback_client_ip_raises_no_alarm(forwarded_for: str) -> None:
    client = make_app(
        limits=RequestRateLimits(),
        settings=make_settings(trusted_proxy_hops=1),
    )

    with structlog.testing.capture_logs() as logs:
        client.get("/api/auth/login", headers={"X-Forwarded-For": forwarded_for})

    assert [line for line in logs if line["event"] == "rate_limit.client_ip.private"] == []


def test_refusal_through_the_redis_limiter() -> None:
    """The middleware over the real Redis adapter (the cloud setup)."""
    limiter = RedisRateLimiter(require_redis())
    client = make_app(
        limits=RequestRateLimits(sign_in_per_min=2),
        settings=make_settings(trusted_proxy_hops=1),
        limiter=limiter,
    )
    # A fresh address per run: Redis keys outlive a test.
    n = uuid.uuid4().int
    ip = f"8.{n % 256}.{(n >> 8) % 256}.{(n >> 16) % 256}"
    headers = {"X-Forwarded-For": ip}

    statuses = [client.get("/api/auth/login", headers=headers).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


def test_preflight_requests_are_not_counted() -> None:
    client = make_app(limits=RequestRateLimits(dashboard_per_min=1))
    for _ in range(5):
        assert client.options("/api/upstreams").status_code == 200

    assert client.get("/api/upstreams").status_code == 200


def test_switched_off_never_refuses() -> None:
    client = make_app(
        limits=RequestRateLimits(enabled=False, sign_in_per_min=1),
    )

    statuses = {client.get("/api/auth/login").status_code for _ in range(5)}

    assert statuses == {200}


# ── The real app ──────────────────────────────────────────────────────

CONFIG_JSON = json.dumps({
    "upstreams": {},
    "roles": {"admin": {"is_admin": True, "settings": {"mcp_access": {"mcps": {}}}}},
    "users": {"admin@example.com": {"role": "admin"}},
})


def make_real_app(tmp_path: Path, **overrides: Any) -> TestClient:
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(CONFIG_JSON)
    settings = make_settings(
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        server_url="http://localhost:8000",
        test_mode=True,
        **overrides,
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        app = create_app(settings)
    return TestClient(app)


def route_paths(routes: list[BaseRoute], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for route in routes:
        if isinstance(route, Mount):
            paths |= route_paths(list(route.routes), prefix + route.path)
        elif isinstance(route, Route):
            paths.add(prefix + route.path)
        else:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(prefix + path)
    return paths


def test_every_listed_sign_in_path_is_a_real_route(tmp_path: Path) -> None:
    """Drift guard: renaming a sign-in route without updating the list
    would silently move it to the looser dashboard limit."""
    client = make_real_app(tmp_path)
    paths = route_paths(list(client.app.routes))  # type: ignore[attr-defined]

    assert set(SIGN_IN_API_PATHS) <= paths
    assert EVENT_STREAM_PATHS <= paths
    assert "/api/admin/upstreams/{upstream_id}/logs/stream" in paths
    # (``/revoke`` is classified too, but the SDK only mounts it when
    # token revocation is enabled, which the gateway doesn't do.)
    for gateway_path in (
        "/mcp/authorize", "/mcp/token", "/mcp/register",
        "/mcp/oauth/google/callback", "/mcp/oauth/consent",
    ):
        assert gateway_path in paths
        assert classify(gateway_path) == SignInGroup.mcp_oauth


def test_real_app_refuses_sign_in_and_dashboard_over_their_limits(
    tmp_path: Path,
) -> None:
    client = make_real_app(
        tmp_path,
        rate_limit_sign_in_per_min=2,
        rate_limit_dashboard_per_min=2,
    )
    assert client.get("/api/orgs/default/public").status_code == 200
    assert client.get("/api/orgs/default/public").status_code == 200
    sign_in_refused = client.get("/api/orgs/default/public")

    assert client.get("/api/config/features").status_code == 200
    assert client.get("/api/config/features").status_code == 200
    dashboard_refused = client.get("/api/config/features")

    assert sign_in_refused.status_code == 429
    assert sign_in_refused.json()["error"] == "rate_limited"
    assert dashboard_refused.status_code == 429
    # Inside CORS: a browser-based client can still read the refusal.
    with_origin = client.get(
        "/api/config/features", headers={"Origin": "https://inspector.example"},
    )
    assert with_origin.status_code == 429
    assert with_origin.headers["access-control-allow-origin"] == "*"


def test_retry_after_is_readable_by_browser_clients(tmp_path: Path) -> None:
    client = make_real_app(tmp_path, rate_limit_sign_in_per_min=1)
    origin = {"Origin": "https://inspector.example"}
    client.get("/api/orgs/default/public", headers=origin)

    refused = client.get("/api/orgs/default/public", headers=origin)

    assert refused.status_code == 429
    exposed = refused.headers.get("access-control-expose-headers", "").lower()
    assert "retry-after" in exposed
