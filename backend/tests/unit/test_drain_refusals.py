"""What the SIGTERM drain answers a request it refuses, on the real app.

The drain refuses new requests while a deploy shuts the backend down. Its
answer must follow the same rules as the rate limiter's refusals:

- MCP OAuth endpoints (``/mcp/.../token`` ...) get an OAuth error the MCP
  SDK knows. A plain ``503 {"detail": ...}`` made the TypeScript SDK
  treat a refused token refresh as a server error, drop the saved
  sign-in and start an interactive sign-in.
- A refusal carries the CORS headers a browser-based MCP client needs to
  read it: the drain runs inside the CORS middleware.
- The dashboard's live stream (EventSource) is let through: a browser
  never retries an EventSource that got a non-200 answer, so the tab
  froze until reloaded.
- Everything else still gets a 503 the dashboard can read.

The app is built with ``create_app`` (standalone, file stores) and put in
draining state through its own ``DrainCoordinator``; no lifespan is
needed because the drain answers before any route.
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.lifecycle import DrainCoordinator, DrainMiddleware
from tests.unit.test_mcp_endpoints_start_at_boot import make_standalone_settings

BROWSER_ORIGIN = "https://inspector.example"


async def make_draining_app(tmp_path: Path) -> FastAPI:
    app = create_app(make_standalone_settings(tmp_path))
    drain = next(
        m.kwargs["drain"] for m in app.user_middleware if m.cls is DrainMiddleware
    )
    assert isinstance(drain, DrainCoordinator)
    await drain.drain()  # nothing in flight: draining at once
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
    )


def middleware_order(app: FastAPI) -> list[str]:
    """Outermost first."""
    return [m.cls.__name__ for m in app.user_middleware]  # type: ignore[union-attr]


async def test_the_drain_runs_inside_cors(tmp_path: Path) -> None:
    order = middleware_order(await make_draining_app(tmp_path))

    assert order.index("DrainMiddleware") > order.index("CORSMiddleware"), order


async def test_a_token_refresh_during_the_drain_gets_an_oauth_error(
    tmp_path: Path,
) -> None:
    app = await make_draining_app(tmp_path)
    async with make_client(app) as client:
        resp = await client.post(
            "/mcp/token",
            data={"grant_type": "refresh_token", "refresh_token": "r"},
            headers={"Origin": BROWSER_ORIGIN},
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable", resp.text
    assert resp.headers.get("retry-after")
    assert resp.headers.get("access-control-allow-origin"), dict(resp.headers)


async def test_the_dashboard_event_stream_is_not_refused_during_the_drain(
    tmp_path: Path,
) -> None:
    app = await make_draining_app(tmp_path)
    async with make_client(app) as client:
        resp = await client.get(
            "/api/events", headers={"Accept": "text/event-stream"},
        )

    # Unauthenticated, so the route answers 401; a 503 from the drain
    # means the request never reached it.
    assert resp.status_code == 401, (resp.status_code, resp.text)


async def test_a_dashboard_call_during_the_drain_gets_a_readable_503(
    tmp_path: Path,
) -> None:
    app = await make_draining_app(tmp_path)
    async with make_client(app) as client:
        resp = await client.get(
            "/api/admin/upstreams", headers={"Origin": BROWSER_ORIGIN},
        )

    assert resp.status_code == 503
    assert resp.json() == {"detail": "Server is shutting down"}
    assert resp.headers.get("retry-after")
    assert resp.headers.get("access-control-allow-origin")
