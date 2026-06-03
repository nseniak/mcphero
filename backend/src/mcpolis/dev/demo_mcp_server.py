# pyright: reportUnusedFunction=false
"""Bundled "kitchen sink" demo MCP server for MCP Hero.

This module replaces the old ``tests/e2e/test_mcp_server.py`` fixture. It
exposes the full upstream surface area we care about exercising:

* echo / add / greet — preserved for the e2e suite.
* a static ``test://hello-world`` resource and a templated
  ``test://greeting/{name}`` resource.
* a ``greet_prompt`` and a no-arg ``summarize_clicks_prompt``.
* five MCP-Apps widgets (inline / fullscreen / pip / counter /
  solar) ported from the POC at ``mcp-app-poc``.

The server can run two ways:

1. **Mounted into the main backend.** ``build_demo_app(public_url=...)``
   returns a Starlette app that gets mounted at ``/dev/mcp-demo`` —
   the gateway then registers the demo as an upstream pointing at
   ``<server_url>/dev/mcp-demo/mcp``. Same origin → CSP / tunneling
   are trivially satisfied via ``MCPOLIS_PUBLIC_URL``.

2. **Standalone via** ``python -m mcpolis.dev.demo_mcp_server`` —
   serves on ``127.0.0.1:9999`` so existing e2e tooling (which targets
   that port for service-account auth) keeps working without changes.

The demo follows every gotcha catalogued in
``mcp-app-poc/FINDINGS.md`` — DNS rebinding off, dual ``_meta`` keys,
exact MIME, CSP, iframe dimensions, bundled SDK URL, etc.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ServerCapabilities
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Stable identifier the gateway uses when seeding the demo as an
# upstream. The widget URI stems use this too — keep them in sync so
# the round-trip ``ui://mcp-demo/...`` → ``resources/read`` is
# self-consistent.
DEMO_UPSTREAM_ID = "mcp-demo"
DEMO_UPSTREAM_DISPLAY_NAME = "MCP Hero demo"

WIDGET_MIME = "text/html;profile=mcp-app"
WIDGETS_DIR = Path(__file__).parent / "widgets"

INLINE_WIDGET_URI = "ui://mcp-demo/widget/inline"
FULLSCREEN_WIDGET_URI = "ui://mcp-demo/widget/fullscreen"
PIP_WIDGET_URI = "ui://mcp-demo/widget/pip"
COUNTER_WIDGET_URI = "ui://mcp-demo/widget/counter"
SOLAR_WIDGET_URI = "ui://mcp-demo/widget/solar"

# Map widget name (URL segment) → (URI, JS filename). Used both for
# the resource registration and for the unit-test invariant that every
# widget tool's ``_meta.ui.resourceUri`` resolves to a JS file on disk.
WIDGET_REGISTRY: dict[str, tuple[str, str]] = {
    "inline": (INLINE_WIDGET_URI, "inline.js"),
    "fullscreen": (FULLSCREEN_WIDGET_URI, "fullscreen.js"),
    "pip": (PIP_WIDGET_URI, "pip.js"),
    "counter": (COUNTER_WIDGET_URI, "counter.js"),
    "solar": (SOLAR_WIDGET_URI, "solar.js"),
}

DEMO_INSTRUCTIONS = (
    "Demo upstream for the MCP Hero test suite. Provides tools, a "
    "resource, a prompt, and five widget kinds (inline, fullscreen, "
    "pip, counter, solar)."
)


def _shell_html(public_url: str, widget_name: str) -> str:
    """Stable 4-line shell that dynamically imports the real widget JS.

    The MCP resource URI never changes; the JS endpoint is hot-reloaded
    on every iframe render via the ``?t=`` cache-buster. See
    FINDINGS §12 ("shell + hot-reloaded JS pattern") for why this is
    the recommended layout — it sidesteps Claude's per-connector tool
    cache that otherwise pins ``_meta.ui.resourceUri`` at first probe.
    """
    base_url = public_url.rstrip("/")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        "<body><script type=\"module\">"
        f"const m = await import('{base_url}/dev/mcp-demo/widget/{widget_name}.js?t=' + Date.now());"
        "await m.default(document.body);"
        "</script></body></html>"
    )


def _widget_csp(public_url: str) -> dict[str, Any]:
    """CSP allowing the dynamic-import target + the bundled SDK origin
    + WebSockets back to our public URL."""
    base_url = public_url.rstrip("/")
    return {
        "resourceDomains": [base_url, "https://unpkg.com"],
        "connectDomains": [
            base_url,
            base_url.replace("https://", "wss://").replace("http://", "ws://"),
        ],
    }


def _widget_meta_for_resource(public_url: str) -> dict[str, Any]:
    return {"ui": {"csp": _widget_csp(public_url)}}


def _widget_meta_for_tool(uri: str) -> dict[str, Any]:
    """Tool ``_meta`` with both modern + legacy widget URI keys.

    The ext-apps SDK normalizer (``ext-apps@1.7.0``) emits both shapes
    unconditionally (FINDINGS §3); we mirror that so any
    spec-compliant client picks one up regardless of which key it
    consumes.
    """
    return {"ui": {"resourceUri": uri}, "ui/resourceUri": uri}


# ── Solar-system click ledger + WebSocket pubsub ──────────────────────
#
# The solar widget calls ``record_planet_click`` from inside the iframe;
# the model retrieves the click via ``get_last_clicked_planet`` and
# answers via ``submit_explanation`` which broadcasts the text to every
# subscriber on ``/ws/explanations``. Process-global today; per-user
# scoping is a follow-up (per the plan's "decisions deferred").
_explanation_subscribers: dict[str, set[WebSocket]] = {}
_explanation_lock = asyncio.Lock()
_last_click: dict[str, str] | None = None
_click_lock = asyncio.Lock()


def build_demo_mcp(public_url: str) -> FastMCP:
    """Build the FastMCP server with all tools / resources / prompts.

    Pulled out so the unit smoke test can import the server without
    spinning up Starlette / uvicorn.
    """
    mcp = FastMCP(
        DEMO_UPSTREAM_ID,
        instructions=DEMO_INSTRUCTIONS,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Tunneled/proxied origins (ngrok, Vite dev proxy) hand the
        # backend a non-loopback Host. FastMCP's default allow-list
        # rejects those with 421. Disabling DNS-rebinding protection is
        # required for the demo to work behind the Vite proxy at
        # ``dev.example.com``. See FINDINGS §1.2.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    # ── E2E-fixture tools (preserved from the old test_mcp_server.py)
    @mcp.tool()
    def echo(message: str) -> str:
        """Echo back the message."""
        return message

    @mcp.tool()
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    @mcp.tool()
    def greet(name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"

    # ── Widget tools ───────────────────────────────────────────────
    @mcp.tool(
        name="open_inline_widget",
        title="Open inline widget",
        description=(
            "Opens an inline widget panel in the chat. The widget has "
            "a button that sends a follow-up prompt back to the chat."
        ),
        meta=_widget_meta_for_tool(INLINE_WIDGET_URI),
    )
    def open_inline_widget() -> str:
        return "Widget opened. Use the button inside it to send a follow-up prompt."

    @mcp.tool(
        name="open_fullscreen_widget",
        title="Open fullscreen widget",
        description=(
            "Opens an immersive fullscreen widget that takes over the "
            "chat view."
        ),
        meta=_widget_meta_for_tool(FULLSCREEN_WIDGET_URI),
    )
    def open_fullscreen_widget() -> str:
        return "Fullscreen widget opened. Use the button inside it to send a follow-up prompt."

    @mcp.tool(
        name="open_pip_widget",
        title="Open PiP widget",
        description=(
            "Opens a picture-in-picture widget. Falls back to inline "
            "on hosts that don't advertise pip (Claude Desktop today)."
        ),
        meta=_widget_meta_for_tool(PIP_WIDGET_URI),
    )
    def open_pip_widget() -> str:
        return "PiP widget opened — floats on hosts that support pip, otherwise inline."

    @mcp.tool(
        name="open_counter_widget",
        title="Open backend-streamed counter widget",
        description=(
            "Opens a widget that displays a counter streamed from the "
            "backend over a WebSocket."
        ),
        meta=_widget_meta_for_tool(COUNTER_WIDGET_URI),
    )
    def open_counter_widget() -> str:
        return "Counter widget opened. It will stream ticks from the backend."

    @mcp.tool(
        name="open_solar_system_widget",
        title="Open solar-system widget",
        description=(
            "Opens a fullscreen solar-system diagram. Clicking a "
            "planet records the click server-side. When the user "
            "asks about the widget, call `get_last_clicked_planet` "
            "to retrieve the planet + request_id, then deliver the "
            "answer via `submit_explanation(request_id, text)`."
        ),
        meta=_widget_meta_for_tool(SOLAR_WIDGET_URI),
    )
    def open_solar_system_widget() -> str:
        return "Solar system opened. Click any planet, then ask the assistant about it."

    # ── Widget callback tools (for the solar-system loop) ────────
    @mcp.tool(
        name="record_planet_click",
        title="[widget-internal] record a planet click",
        description=(
            "Called by the solar-system widget when the user clicks a "
            "planet. Not intended to be called by the model directly."
        ),
    )
    async def record_planet_click(planet_id: str, request_id: str) -> str:
        global _last_click
        async with _click_lock:
            _last_click = {"planet_id": planet_id, "request_id": request_id}
        return "ok"

    @mcp.tool(
        name="get_last_clicked_planet",
        title="Get the last planet the user clicked",
        description=(
            "Returns the planet most recently clicked in the "
            "solar-system widget along with the request_id the widget "
            "is waiting on."
        ),
    )
    async def get_last_clicked_planet() -> str:
        async with _click_lock:
            click = _last_click
        if click is None:
            return json.dumps({"status": "no_click"})
        return json.dumps({
            "status": "ok",
            "planet_id": click["planet_id"],
            "request_id": click["request_id"],
        })

    @mcp.tool(
        name="submit_explanation",
        title="Deliver an explanation to the solar-system widget",
        description=(
            "Returns a short explanation for an object the user "
            "clicked. Keep it concise (2-4 sentences, no markdown)."
        ),
    )
    async def submit_explanation(request_id: str, text: str) -> str:
        async with _explanation_lock:
            subs = list(_explanation_subscribers.get(request_id, ()))
            _explanation_subscribers.pop(request_id, None)
        payload = json.dumps(
            {"type": "explanation", "request_id": request_id, "text": text},
        )
        delivered = 0
        for ws in subs:
            try:
                await ws.send_text(payload)
                delivered += 1
            except Exception:  # noqa: BLE001 — broken socket is fine to skip
                pass
        return f"ok (delivered to {delivered} widget(s))"

    # ── Static + templated resources ───────────────────────────────
    @mcp.resource(
        "test://hello-world",
        name="hello-world",
        description="A static greeting resource for E2E tests.",
        mime_type="text/plain",
    )
    def hello_world_resource() -> str:
        return "Hello, world!"

    @mcp.resource(
        "test://greeting/{name}",
        name="greeting",
        description="Synthesizes a greeting for the given name.",
        mime_type="text/plain",
    )
    def greeting_template(name: str) -> str:
        return f"Hello, {name}!"

    # ── Widget resource shells ─────────────────────────────────────
    @mcp.resource(
        INLINE_WIDGET_URI,
        name="inline-widget",
        title="Inline demo widget",
        mime_type=WIDGET_MIME,
        meta=_widget_meta_for_resource(public_url),
    )
    def inline_widget_resource() -> str:
        return _shell_html(public_url, "inline")

    @mcp.resource(
        FULLSCREEN_WIDGET_URI,
        name="fullscreen-widget",
        title="Fullscreen demo widget",
        mime_type=WIDGET_MIME,
        meta=_widget_meta_for_resource(public_url),
    )
    def fullscreen_widget_resource() -> str:
        return _shell_html(public_url, "fullscreen")

    @mcp.resource(
        PIP_WIDGET_URI,
        name="pip-widget",
        title="PiP demo widget",
        mime_type=WIDGET_MIME,
        meta=_widget_meta_for_resource(public_url),
    )
    def pip_widget_resource() -> str:
        return _shell_html(public_url, "pip")

    @mcp.resource(
        COUNTER_WIDGET_URI,
        name="counter-widget",
        title="Counter demo widget",
        mime_type=WIDGET_MIME,
        meta=_widget_meta_for_resource(public_url),
    )
    def counter_widget_resource() -> str:
        return _shell_html(public_url, "counter")

    @mcp.resource(
        SOLAR_WIDGET_URI,
        name="solar-widget",
        title="Solar-system widget",
        mime_type=WIDGET_MIME,
        meta=_widget_meta_for_resource(public_url),
    )
    def solar_widget_resource() -> str:
        return _shell_html(public_url, "solar")

    # ── Prompts ─────────────────────────────────────────────────────
    @mcp.prompt()
    def greet_prompt(name: str) -> list[base.Message]:
        """Render a single user message greeting *name*."""
        return [base.UserMessage(content=f"hello {name}")]

    @mcp.prompt()
    def summarize_clicks_prompt() -> list[base.Message]:
        """Multi-message no-arg prompt: ask the model to summarize
        recent solar-system clicks. Exercises both branches at once."""
        return [
            base.UserMessage(
                content=(
                    "I've been clicking around in the solar-system widget. "
                    "Summarize what I've selected most recently."
                ),
            ),
            base.AssistantMessage(
                content=(
                    "I'll call `get_last_clicked_planet` and report back."
                ),
            ),
        ]

    # ── Advertise the MCP-Apps extension on initialize ─────────────
    # ``ServerCapabilities`` has ``model_config.extra = "allow"`` so we
    # round-trip via ``model_dump`` + ``model_validate`` to inject
    # ``extensions``. Same trick as the POC; the gateway's Part A
    # aggregation picks this up and re-advertises it downstream.
    server = mcp._mcp_server  # pyright: ignore[reportPrivateUsage]
    _original_init_options = server.create_initialization_options

    def _init_options_with_ui_ext(*args: Any, **kwargs: Any) -> Any:
        opts = _original_init_options(*args, **kwargs)
        caps_dict = opts.capabilities.model_dump(
            exclude_none=True, by_alias=True,
        )
        caps_dict["extensions"] = {
            "io.modelcontextprotocol/ui": {"mimeTypes": [WIDGET_MIME]},
        }
        return opts.model_copy(
            update={
                "capabilities": ServerCapabilities.model_validate(caps_dict),
            },
        )

    server.create_initialization_options = _init_options_with_ui_ext

    return mcp


def _widget_js_route_factory(public_url: str):  # type: ignore[no-untyped-def]
    """Return an ASGI handler that serves the widget JS files on disk.

    ``__PUBLIC_URL__`` placeholders inside each file are replaced with
    the real ``public_url`` so widgets can phone home (e.g. WebSockets)
    without hard-coding it at build time.
    """

    base_url = public_url.rstrip("/")

    async def widget_js(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in WIDGET_REGISTRY:
            return JSONResponse(
                {"error": f"unknown widget: {name}"}, status_code=404,
            )
        path = WIDGETS_DIR / WIDGET_REGISTRY[name][1]
        if not path.is_file():
            return JSONResponse(
                {"error": f"widget JS missing: {WIDGET_REGISTRY[name][1]}"},
                status_code=500,
            )
        body = path.read_text(encoding="utf-8").replace(
            "__PUBLIC_URL__", base_url,
        )
        return Response(
            body,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )

    return widget_js


async def _counter_ws(ws: WebSocket) -> None:
    """Tick once per second. Clean WebSocket — proxies don't buffer
    these the way they buffer SSE (FINDINGS §11)."""
    await ws.accept()
    n = 0
    try:
        while True:
            await ws.send_text(str(n))
            n += 1
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return


async def _explanations_ws(ws: WebSocket) -> None:
    """Widget subscribes to a request_id; ``submit_explanation`` pushes
    the model's answer back through this socket."""
    await ws.accept()
    my_subscriptions: set[str] = set()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                parsed: object = json.loads(raw)
            except Exception:  # noqa: BLE001 — bad JSON, ignore tick
                continue
            if not isinstance(parsed, dict):
                continue
            action = parsed.get("subscribe")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            if isinstance(action, str) and action:
                async with _explanation_lock:
                    _explanation_subscribers.setdefault(action, set()).add(ws)
                my_subscriptions.add(action)
    except WebSocketDisconnect:
        pass
    finally:
        async with _explanation_lock:
            for rid in my_subscriptions:
                subs = _explanation_subscribers.get(rid)
                if subs is None:
                    continue
                subs.discard(ws)
                if not subs:
                    _explanation_subscribers.pop(rid, None)


def build_demo_app(public_url: str) -> tuple[Starlette, FastMCP]:
    """Build the Starlette app to mount at ``/dev/mcp-demo`` together
    with the underlying FastMCP instance.

    Routing layout (relative to the mount):

    * ``/mcp``                  → MCP Streamable HTTP endpoint.
    * ``/widget/{name}.js``     → hot-reloaded widget JS.
    * ``/ws/counter``           → counter widget tick stream.
    * ``/ws/explanations``      → solar widget pubsub.

    Why we return the FastMCP instance too: ``streamable_http_app``
    registers a lifespan that calls
    ``StreamableHTTPSessionManager.run()``, and FastAPI's ``mount``
    (and Starlette's ``Mount``) does NOT propagate child lifespans
    to the parent — Starlette only honors its own
    ``Starlette(lifespan=...)`` callback. Without entering the demo's
    ``session_manager.run()`` ourselves, the first MCP request fails
    with "Task group is not initialized." The backend's lifespan
    threads ``demo_mcp.session_manager.run()`` into the existing
    ``async with`` chain alongside the gateway's session managers
    (see ``app.py``).

    We extend the Starlette app FastMCP returns rather than wrapping
    it for the same reason: a parent Starlette wrapping wouldn't
    propagate the inner lifespan either. Mirrors the pattern the
    upstream POC uses (``mcp-app-poc/server.py:build_app``).
    """
    mcp = build_demo_mcp(public_url)
    app: Starlette = mcp.streamable_http_app()
    app.router.routes.append(
        Route(
            "/widget/{name}.js",
            _widget_js_route_factory(public_url),
            methods=["GET"],
        ),
    )
    app.router.routes.append(WebSocketRoute("/ws/counter", _counter_ws))
    app.router.routes.append(
        WebSocketRoute("/ws/explanations", _explanations_ws),
    )
    return app, mcp


def main() -> None:
    """Standalone entrypoint — run as ``python -m
    mcpolis.dev.demo_mcp_server``. Listens on ``127.0.0.1:9999`` by
    default so legacy e2e harnesses keep working; the sharding
    orchestrator overrides via ``MCPOLIS_DEMO_PORT`` so each shard's
    fake lives on its own port and shards never trample each other."""
    import uvicorn

    port = int(os.environ.get("MCPOLIS_DEMO_PORT", "9999"))
    public_url = os.environ.get(
        "MCPOLIS_DEMO_PUBLIC_URL", f"http://127.0.0.1:{port}",
    )
    app, _ = build_demo_app(public_url)
    uvicorn.run(
        app, host="127.0.0.1", port=port, log_level="warning",
    )


if __name__ == "__main__":
    main()
