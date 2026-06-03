"""End-to-end integration test for the E2B-backed sandbox path.

Stands up real sandboxes against the live E2B API, drives full MCP
sessions through ``E2BSandboxService`` (the same code path the
production backend goes through), and exercises every failure mode
that's worth catching in pre-deploy verification.

Why a script and not a pytest file: this exercises external state
(spends real money, takes minutes, depends on E2B account templates),
so it must be run-on-demand, not in CI. The output table at the end
gives operator-friendly per-scenario timing and a single PASS/FAIL
banner.

Coverage at V1 (matches the conversation in 2026-05-01):

* ``smoke_npx`` — happy path with ``server-everything`` via npx.
* ``smoke_uvx`` — happy path with a Python MCP via uvx (cold-install
  timing is meaningfully different from npx and exposes a different
  template).
* ``reattach_after_idle_pause`` — **the headline regression.** Opens
  a session with ``MCPOLIS_E2B_IDLE_PAUSE_SECONDS=30``, waits past
  the window, makes a tool call, and asserts both that the call
  succeeds AND that ``sandbox.e2b.reattach.ok`` was emitted (the
  bug fixed in this PR: streaming RPC severed at pause, never
  reattached without the new code path).
* ``bad_command`` — wrong npm package name → ``initialize`` times
  out → status reflects an error rather than "Connected".
* ``concurrent_calls`` — three parallel ``call_tool`` requests on a
  single session, verifies the JSON-RPC id demux delivers responses
  to the right caller.
* ``status_reflects_connection`` — ``client_manager.is_connected``
  flips True→False as the session opens/closes (this is the same
  predicate the admin upstreams route renders for the UI).
* ``sse_log_stream`` — ``LogBuffer.subscribe`` (the async iterator
  the ``/logs/stream`` SSE endpoint forwards verbatim) emits the
  npm/uvx cold-install chatter as it arrives.

Simplifications worth knowing:

* Status verification calls ``client_manager.is_connected`` directly
  instead of hitting ``GET /api/admin/upstreams/{id}`` over HTTP.
  The route is a thin shell over that predicate plus a couple of
  OAuth-specific checks not relevant here — the predicate is what
  determines what the UI shows.
* SSE verification reads from ``LogBuffer.subscribe`` directly
  rather than connecting an HTTP client to ``/logs/stream``. The
  route's streaming body iterates exactly that subscription and
  wraps each chunk in an ``data: ...`` SSE frame; what's delivered
  is byte-for-byte the same.

Run with::

    export MCPOLIS_E2B_API_KEY=...
    bash backend/tests/integration/run-e2b-real-e2e.sh

Total wall clock: ~3-5 min; estimated cost: ~$0.05 of E2B compute.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import structlog
from structlog.typing import EventDict, WrappedLogger

# Make ``src`` importable when run directly (``python integration/e2b_real_e2e.py``).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_TESTS = os.path.normpath(os.path.join(_HERE, "..", "tests"))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from mcp.client.session import ClientSession  # noqa: E402

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (  # noqa: E402
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import (  # noqa: E402
    E2BSandboxService,
    RealE2BClient,
)
from mcpolis.adapters.upstream_clients.client_manager import (  # noqa: E402
    UpstreamClientManager,
)
from mcpolis.adapters.upstream_clients.log_buffer import LogBuffer  # noqa: E402
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig  # noqa: E402
from mcpolis.domain.model.upstream import (  # noqa: E402
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.services.sandbox_resolver import SandboxResolver  # noqa: E402
from mcpolis.domain.services.sandbox_service import SandboxResources  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("MCPOLIS_E2B_API_KEY") or os.environ.get("E2B_API_KEY")
RUN_ID = uuid.uuid4().hex[:8]
# Idle-pause override. Short enough that the reattach scenario takes
# seconds, not the production-default 5 min. Anything below ~10s
# risks the box snapshotting mid-init on a slow cold-pull.
IDLE_PAUSE_SECONDS = 30
# Buffer past the idle window. Empirically auto-pause fires within a
# few seconds of the configured deadline; +5s gives consistent reproc.
REATTACH_WAIT_SECONDS = IDLE_PAUSE_SECONDS + 5
# Bound on how long ``initialize`` may take. Cold npm/uvx installs
# inside a fresh sandbox can stretch — the existing real-SDK suite
# uses 120s and it's been adequate.
INITIALIZE_TIMEOUT = 120.0
TOOL_CALL_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Structlog: route the service's logs into a buffer so the reattach
# assertion can grep for ``sandbox.e2b.reattach.ok`` without parsing
# stderr text.
# ---------------------------------------------------------------------------


class _LogCapture:
    """Captures every structlog event emitted during the run.

    Implemented as a structlog processor so it sees the same event
    dict the production logger does — including the hand-typed event
    keys we assert on (``sandbox.e2b.reattach.ok`` etc.). Keeping a
    list rather than a queue lets per-scenario assertions slice by
    timestamp.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(
        self,
        logger: WrappedLogger,
        method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        del logger, method_name
        self.events.append(dict(event_dict))
        return event_dict

    def has_event(self, name: str, *, since_ns: int | None = None) -> bool:
        for ev in self.events:
            if ev.get("event") != name:
                continue
            if since_ns is None:
                return True
            ts = ev.get("_recorded_ns")
            if isinstance(ts, int) and ts >= since_ns:
                return True
        return False

    def find_events(
        self, name: str, *, since_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all matching events captured at or after
        ``since_ns``. Used by reattach scenarios to extract per-event
        timing fields (e.g. ``reattach_duration_ms``) into the
        scenario's timings table."""
        out: list[dict[str, Any]] = []
        for ev in self.events:
            if ev.get("event") != name:
                continue
            if since_ns is not None:
                ts = ev.get("_recorded_ns")
                if not (isinstance(ts, int) and ts >= since_ns):
                    continue
            out.append(ev)
        return out


def _stamp_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict,
) -> EventDict:
    del logger, method_name
    event_dict["_recorded_ns"] = time.monotonic_ns()
    return event_dict


_log_capture = _LogCapture()
structlog.configure(
    processors=[
        _stamp_processor,
        _log_capture,
        structlog.processors.KeyValueRenderer(
            key_order=["event"], drop_missing=True,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resources() -> SandboxResources:
    """Smallest published combo to keep cost minimal."""
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def _make_service(*, on_timeout_seconds: int = IDLE_PAUSE_SECONDS) -> E2BSandboxService:
    assert API_KEY, "API_KEY must be checked before calling _make_service()"
    return E2BSandboxService(
        RealE2BClient(api_key=API_KEY),
        mcpolis_instance=f"e2e-{RUN_ID}",
        on_timeout_seconds=on_timeout_seconds,
    )


def _upstream(
    upstream_id: str,
    *,
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id=upstream_id,
        display_name=upstream_id,
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command=command, args=args, env=env or {},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    timings_ms: dict[str, float] = field(default_factory=dict[str, float])
    error: str | None = None
    notes: list[str] = field(default_factory=list[str])


Scenario = Callable[[], Awaitable[ScenarioResult]]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def _run_full_session(
    *,
    name: str,
    service: E2BSandboxService,
    upstream: UpstreamDefinition,
    do_call_tool: bool,
) -> ScenarioResult:
    """Shared driver for the two smoke scenarios.

    Tracks four sub-timings: ``open`` (sandbox.create + run_command),
    ``initialize`` (MCP handshake + cold install), ``list_tools``,
    ``call_tool`` (skipped when ``do_call_tool=False``)."""
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-{name}"
    try:
        t0 = time.monotonic()
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["open_ms"] = (time.monotonic() - t0) * 1000

            session = ClientSession(read_stream, write_stream)
            async with session:
                t1 = time.monotonic()
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                timings["initialize_ms"] = (time.monotonic() - t1) * 1000
                if not init_result.serverInfo.name:
                    return ScenarioResult(
                        name=name, passed=False,
                        timings_ms=timings,
                        error="serverInfo.name was empty",
                    )

                t2 = time.monotonic()
                tools = await asyncio.wait_for(
                    session.list_tools(), timeout=TOOL_CALL_TIMEOUT,
                )
                timings["list_tools_ms"] = (time.monotonic() - t2) * 1000
                if not tools.tools:
                    return ScenarioResult(
                        name=name, passed=False, timings_ms=timings,
                        error="list_tools returned empty",
                    )

                if do_call_tool:
                    # Pick the first tool with no required arguments,
                    # else fall back to ``echo`` semantics. Both
                    # server-everything and most uvx Python MCPs expose
                    # something callable with no args.
                    callable_tool = next(
                        (
                            t for t in tools.tools
                            if not t.inputSchema.get("required")
                        ),
                        None,
                    )
                    if callable_tool is None:
                        return ScenarioResult(
                            name=name, passed=False, timings_ms=timings,
                            error="no zero-arg tool found",
                        )
                    t3 = time.monotonic()
                    await asyncio.wait_for(
                        session.call_tool(callable_tool.name, {}),
                        timeout=TOOL_CALL_TIMEOUT,
                    )
                    timings["call_tool_ms"] = (time.monotonic() - t3) * 1000
        return ScenarioResult(
            name=name, passed=True, timings_ms=timings,
            notes=[f"tools: {len(tools.tools)}"],
        )
    except Exception as exc:
        captured = errlog.get_output()
        snippet = captured[-400:] if captured else ""
        return ScenarioResult(
            name=name, passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
            notes=[f"sandbox stderr tail: {snippet!r}"] if snippet else [],
        )


async def smoke_npx() -> ScenarioResult:
    service = _make_service()
    upstream = _upstream(
        f"smoke-npx-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    return await _run_full_session(
        name="smoke_npx", service=service, upstream=upstream, do_call_tool=True,
    )


async def smoke_uvx() -> ScenarioResult:
    """Python MCP via uvx. ``mcp-server-time`` exposes one tool
    (``get_current_time``) that REQUIRES a ``timezone`` argument,
    so the shared driver's "find a zero-arg tool" path doesn't fit.
    Hand-rolled to call the tool with a known good arg and verify
    the response shape — exercises the python template + the
    args-required tool-call path that the npx smoke can't reach.
    """
    service = _make_service()
    upstream = _upstream(
        f"smoke-uvx-{RUN_ID}",
        command="uvx",
        args=["mcp-server-time"],
    )
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-smoke-uvx"
    try:
        t0 = time.monotonic()
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["open_ms"] = (time.monotonic() - t0) * 1000
            session = ClientSession(read_stream, write_stream)
            async with session:
                t1 = time.monotonic()
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                timings["initialize_ms"] = (time.monotonic() - t1) * 1000
                if not init_result.serverInfo.name:
                    return ScenarioResult(
                        name="smoke_uvx", passed=False,
                        timings_ms=timings,
                        error="serverInfo.name was empty",
                    )
                t2 = time.monotonic()
                tools = await session.list_tools()
                timings["list_tools_ms"] = (time.monotonic() - t2) * 1000
                # ``mcp-server-time`` provides ``get_current_time``
                # (and sometimes ``convert_time``). Both require a
                # ``timezone`` argument.
                target = next(
                    (t for t in tools.tools if t.name == "get_current_time"),
                    None,
                )
                if target is None:
                    return ScenarioResult(
                        name="smoke_uvx", passed=False, timings_ms=timings,
                        error=(
                            "mcp-server-time no longer exposes "
                            "'get_current_time' — pick a different tool"
                        ),
                    )
                t3 = time.monotonic()
                result = await asyncio.wait_for(
                    session.call_tool(
                        "get_current_time", {"timezone": "UTC"},
                    ),
                    timeout=TOOL_CALL_TIMEOUT,
                )
                timings["call_tool_ms"] = (time.monotonic() - t3) * 1000
                # Response should mention UTC somewhere — proves the
                # arg propagated and the response came back from the
                # right tool.
                blob = " ".join(
                    getattr(c, "text", "")
                    for c in result.content
                    if getattr(c, "type", None) == "text"
                )
                if "UTC" not in blob and "utc" not in blob.lower():
                    return ScenarioResult(
                        name="smoke_uvx", passed=False, timings_ms=timings,
                        error=(
                            f"get_current_time(UTC) response didn't "
                            f"mention UTC: {blob[:200]!r}"
                        ),
                    )
        return ScenarioResult(
            name="smoke_uvx", passed=True, timings_ms=timings,
            notes=[f"tools: {len(tools.tools)}; UTC time round-tripped"],
        )
    except Exception as exc:
        captured = errlog.get_output()
        snippet = captured[-400:] if captured else ""
        return ScenarioResult(
            name="smoke_uvx", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
            notes=[f"sandbox stderr tail: {snippet!r}"] if snippet else [],
        )


async def reattach_after_idle_pause() -> ScenarioResult:
    """**Headline regression.** The bug fixed in this PR: after E2B
    auto-pauses the sandbox, the SDK's streaming RPC for stdout is
    severed; ``send_stdin`` on the next tool call would succeed and
    the response would never arrive. Fixed by detecting the dead
    stream and reattaching via ``commands.connect(pid)``. This
    scenario sleeps past the idle window, then asserts both that the
    tool call succeeds AND that ``sandbox.e2b.reattach.ok`` fired.
    """
    service = _make_service(on_timeout_seconds=IDLE_PAUSE_SECONDS)
    upstream = _upstream(
        f"reattach-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-reattach"
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                init_t = time.monotonic()
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                timings["initialize_ms"] = (time.monotonic() - init_t) * 1000
                tools = await session.list_tools()

                # Sleep past the idle window — E2B auto-pauses, the
                # SDK's events stream behind run_command dies. Watch
                # task in service.py sets stream_dead.
                wait_start_ns = time.monotonic_ns()
                print(
                    f"  [reattach] sleeping {REATTACH_WAIT_SECONDS}s "
                    f"to provoke E2B auto-pause...",
                    flush=True,
                )
                await asyncio.sleep(REATTACH_WAIT_SECONDS)

                # First call after the pause. With the fix in place,
                # the pump notices stream_dead and reattaches before
                # send_stdin. Without the fix, this hangs forever
                # (which is why we have a TOOL_CALL_TIMEOUT below).
                callable_tool = next(
                    (
                        t for t in tools.tools
                        if not t.inputSchema.get("required")
                    ),
                    None,
                )
                if callable_tool is None:
                    return ScenarioResult(
                        name="reattach_after_idle_pause", passed=False,
                        timings_ms=timings, error="no zero-arg tool found",
                    )
                # Generous timeout: ``commands.connect`` may itself
                # trigger the resume; resume can take several seconds.
                call_t = time.monotonic()
                await asyncio.wait_for(
                    session.call_tool(callable_tool.name, {}),
                    timeout=TOOL_CALL_TIMEOUT * 2,
                )
                timings["call_tool_post_pause_ms"] = (
                    (time.monotonic() - call_t) * 1000
                )

        # Did the reattach actually fire? If the test passed without
        # ``reattach.ok``, the box never auto-paused and the scenario
        # isn't actually exercising the bug — flag it.
        reattach_events = _log_capture.find_events(
            "sandbox.e2b.reattach.ok", since_ns=wait_start_ns,
        )
        if not reattach_events:
            return ScenarioResult(
                name="reattach_after_idle_pause", passed=False,
                timings_ms=timings,
                error=(
                    "tool call succeeded but sandbox.e2b.reattach.ok "
                    "never fired — sandbox likely didn't auto-pause "
                    "in this window. Try increasing the wait or "
                    "lowering MCPOLIS_E2B_IDLE_PAUSE_SECONDS."
                ),
            )
        wake_ms = reattach_events[0].get("reattach_duration_ms")
        if isinstance(wake_ms, (int, float)):
            timings["wake_from_paused_ms"] = float(wake_ms)
        return ScenarioResult(
            name="reattach_after_idle_pause", passed=True,
            timings_ms=timings,
            notes=[
                f"reattach.ok fired after {REATTACH_WAIT_SECONDS}s sleep",
                f"server: {init_result.serverInfo.name}",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="reattach_after_idle_pause", passed=False,
            timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def reattach_via_ucm() -> ScenarioResult:
    """Same regression as ``reattach_after_idle_pause`` but exercised
    through ``UpstreamClientManager.connect_shared`` — the production
    code path. The other scenario opens ``service.session()``
    directly; this one drives the long-lived shared-session pattern
    that prod actually uses, including post-reattach status
    verification (``is_connected`` must STAY True across the auto-
    pause → reattach cycle).
    """
    service = _make_service(on_timeout_seconds=IDLE_PAUSE_SECONDS)
    upstream = _upstream(
        f"reattach-ucm-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    manager = _make_ucm(upstream=upstream, service=service)
    timings: dict[str, float] = {}
    try:
        await asyncio.wait_for(
            manager.connect_shared(upstream), timeout=INITIALIZE_TIMEOUT,
        )
        if not manager.is_connected(upstream.id):
            return ScenarioResult(
                name="reattach_via_ucm", passed=False,
                error="not connected after connect_shared",
            )

        wait_start_ns = time.monotonic_ns()
        print(
            f"  [reattach_via_ucm] sleeping {REATTACH_WAIT_SECONDS}s "
            f"to provoke E2B auto-pause...",
            flush=True,
        )
        await asyncio.sleep(REATTACH_WAIT_SECONDS)

        # Drive a tool call via the live ClientSession that
        # ``connect_shared`` parked on the upstream's state-machine
        # record. ``list_tools`` is the cheapest round-trip —
        # assertion is "the streaming RPC came back, response
        # arrived" not "this specific tool exists".
        state = manager.get_state(upstream.id)
        if state is None or state.shared_session is None:
            return ScenarioResult(
                name="reattach_via_ucm", passed=False, timings_ms=timings,
                error=(
                    "no live shared_session on the manager state-machine "
                    "record after connect_shared"
                ),
            )
        live_session = state.shared_session
        call_t = time.monotonic()
        await asyncio.wait_for(
            live_session.list_tools(), timeout=TOOL_CALL_TIMEOUT * 2,
        )
        timings["call_tool_post_pause_ms"] = (
            (time.monotonic() - call_t) * 1000
        )

        # ``is_connected`` must stay True — UCM should not flip
        # the badge to Disconnected just because we paused.
        if not manager.is_connected(upstream.id):
            return ScenarioResult(
                name="reattach_via_ucm", passed=False, timings_ms=timings,
                error=(
                    "is_connected flipped to False after auto-pause "
                    "+ reattach — UI would render Disconnected for a "
                    "session that's actually serving requests"
                ),
            )
        reattach_events = _log_capture.find_events(
            "sandbox.e2b.reattach.ok", since_ns=wait_start_ns,
        )
        if not reattach_events:
            return ScenarioResult(
                name="reattach_via_ucm", passed=False, timings_ms=timings,
                error="reattach.ok did not fire — sandbox didn't pause",
            )
        wake_ms = reattach_events[0].get("reattach_duration_ms")
        if isinstance(wake_ms, (int, float)):
            timings["wake_from_paused_ms"] = float(wake_ms)
        return ScenarioResult(
            name="reattach_via_ucm", passed=True, timings_ms=timings,
            notes=[
                "connect_shared → idle 35s → tools/list via live "
                "session → is_connected stayed True",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="reattach_via_ucm", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        try:
            await manager.stop_all()
        except Exception:
            pass


async def restart_with_reuse() -> ScenarioResult:
    """Simulates a clean stop + start with reuse-on-restart enabled.

    Flow:
      1. Service A: open session, persist live ref, exit cleanly
         (skip kill, sandbox stays alive on E2B).
      2. Service B (fresh process, same persistence repo, same
         ``mcpolis_instance``-style identity): open a new session
         for the same upstream → ``_try_reconnect`` finds the ref,
         calls ``Sandbox.connect(sandbox_id)`` +
         ``commands.connect(pid)``, returns the existing handles.
         No new sandbox is created.

    Asserts:
      - After service A exits, the sandbox is in
        ``running`` (or ``paused`` after auto-pause) state — NOT
        destroyed.
      - When service B opens its session, the sandbox handle's id
        matches the one service A used.
      - No new ``Sandbox.create`` call is made for service B (i.e.
        ``client.list_sandboxes`` reports the same sandbox count
        as before service B opened).
      - Tool call after the reconnect succeeds with the expected
        echo payload (proves the streaming RPC came back).

    Cleanup: explicit kill at the end so the test doesn't leak a
    sandbox.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )

    upstream = _upstream(
        f"reuse-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    client = RealE2BClient(api_key=cast(str, API_KEY))
    timings: dict[str, float] = {}

    # ---- Service A: original boot ----
    service_a = E2BSandboxService(
        client,
        mcpolis_instance=f"e2e-{RUN_ID}",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,
    )
    errlog_a = LogBuffer()
    session_id_a = f"e2e-{RUN_ID}-reuse-a"
    org_id = f"acme-{RUN_ID}"
    try:
        t0 = time.monotonic()
        async with service_a.session(
            session_id=session_id_a,
            org_id=org_id,
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog_a),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["a_open_ms"] = (time.monotonic() - t0) * 1000
            session_a = ClientSession(read_stream, write_stream)
            async with session_a:
                await asyncio.wait_for(
                    session_a.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
            # Mark preserve-on-close before exiting the session
            # context — simulates the lifespan handler's shutdown
            # hook, which is the only path under the kill-on-stop
            # contract that leaves the sandbox + ref alive for the
            # next boot's ``_try_reconnect``.
            service_a.mark_session_preserve_on_close(session_id_a)
        # Service A exited cleanly. The persisted ref carries
        # (sandbox_id, pid); the sandbox is still alive on E2B
        # (kill skipped because the preserve-on-close hook fired).
        ref_after_a = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref_after_a is None or ref_after_a.sandbox_id is None:
            return ScenarioResult(
                name="restart_with_reuse", passed=False,
                timings_ms=timings,
                error="service A didn't persist a live ref",
            )
        original_sandbox_id = ref_after_a.sandbox_id
        original_pid = ref_after_a.pid

        # ---- Service B: simulated restart, same persistence ----
        service_b = E2BSandboxService(
            client,
            mcpolis_instance=f"e2e-{RUN_ID}-b",  # different UUID, like a real restart
            on_timeout_seconds=IDLE_PAUSE_SECONDS,
            persistence=persistence,
            reuse_sandboxes_on_restart=True,
        )
        errlog_b = LogBuffer()
        session_id_b = f"e2e-{RUN_ID}-reuse-b"
        t1 = time.monotonic()
        async with service_b.session(
            session_id=session_id_b,
            org_id=org_id,
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog_b),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["b_reconnect_open_ms"] = (time.monotonic() - t1) * 1000
            # The live handle should be the same sandbox B reattached to.
            live_handle = service_b._live_sandboxes[session_id_b]  # type: ignore[reportPrivateUsage]
            if live_handle.sandbox_id != original_sandbox_id:
                return ScenarioResult(
                    name="restart_with_reuse", passed=False,
                    timings_ms=timings,
                    error=(
                        f"service B opened sandbox "
                        f"{live_handle.sandbox_id!r}, expected "
                        f"{original_sandbox_id!r} (reconnect failed; "
                        f"fell back to fresh create)"
                    ),
                )

            session_b = ClientSession(read_stream, write_stream)
            async with session_b:
                # ClientSession.initialize on the reattached
                # streaming RPC; the MCP process is still the
                # original one from service A, no second cold install.
                await asyncio.wait_for(
                    session_b.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                tools = await session_b.list_tools()
                if not any(t.name == "echo" for t in tools.tools):
                    return ScenarioResult(
                        name="restart_with_reuse", passed=False,
                        timings_ms=timings,
                        error="echo tool missing post-reconnect",
                    )
                payload = f"reuse-{RUN_ID}"
                t2 = time.monotonic()
                result = await asyncio.wait_for(
                    session_b.call_tool("echo", {"message": payload}),
                    timeout=TOOL_CALL_TIMEOUT,
                )
                timings["b_call_tool_ms"] = (time.monotonic() - t2) * 1000
                blob = " ".join(
                    getattr(c, "text", "")
                    for c in result.content
                    if getattr(c, "type", None) == "text"
                )
                if payload not in blob:
                    return ScenarioResult(
                        name="restart_with_reuse", passed=False,
                        timings_ms=timings,
                        error=(
                            f"reuse echo response missing payload: "
                            f"{blob[:120]!r}"
                        ),
                    )
        # ---- Cleanup: kill the reused sandbox so we don't leak. ----
        try:
            await client.kill_sandbox(original_sandbox_id)
        except Exception:
            pass
        return ScenarioResult(
            name="restart_with_reuse", passed=True, timings_ms=timings,
            notes=[
                f"sandbox {original_sandbox_id} survived service A exit",
                f"service B reattached to same sandbox + pid={original_pid}",
                "echo tool call after reconnect verified payload",
            ],
        )
    except Exception as exc:
        # Best-effort cleanup on failure path.
        ref = await persistence.get(org_id=org_id, upstream_id=upstream.id)
        if ref is not None and ref.sandbox_id is not None:
            try:
                await client.kill_sandbox(ref.sandbox_id)
            except Exception:
                pass
        return ScenarioResult(
            name="restart_with_reuse", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def restart_with_fresh() -> ScenarioResult:
    """Simulates a clean stop + start with the
    ``MCPOLIS_E2B_FRESH_SANDBOXES`` operator override invoked
    between the two services.

    Flow:
      1. Service A: open session with reuse-on-restart, persist
         live ref, exit (sandbox survives).
      2. Operator-equivalent: ``service.wipe_for_fresh_restart()``
         on the SAME persistence repo. Should kill the persisted
         sandbox and clear the ref.
      3. Service B: opens a session — the wiped persistence has no
         ref to reattach to, so a fresh ``Sandbox.create`` happens.
         New sandbox id ≠ A's sandbox id.

    Asserts:
      - After wipe: persisted ref is gone AND the original sandbox
        is no longer ``running`` on E2B (kill confirmed via
        ``list_sandboxes``).
      - Service B's new sandbox has a different id from service A's.
      - Tool call on service B's fresh session works (proves
        the new sandbox is healthy).

    This is the pre-deploy "I want a clean slate" override every
    operator wants but has to be explicit about.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )

    upstream = _upstream(
        f"fresh-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    client = RealE2BClient(api_key=cast(str, API_KEY))
    timings: dict[str, float] = {}
    org_id = f"acme-{RUN_ID}"

    service_a = E2BSandboxService(
        client,
        mcpolis_instance=f"e2e-{RUN_ID}-fresh-a",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,
    )
    original_sandbox_id: str | None = None
    try:
        async with service_a.session(
            session_id=f"e2e-{RUN_ID}-fresh-a",
            org_id=org_id,
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, LogBuffer()),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session_a = ClientSession(read_stream, write_stream)
            async with session_a:
                await asyncio.wait_for(
                    session_a.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
            # Preserve so wipe_for_fresh_restart has a live ref +
            # sandbox to wipe (kill-on-stop default would otherwise
            # delete both before the wipe step asserts on them).
            service_a.mark_session_preserve_on_close(
                f"e2e-{RUN_ID}-fresh-a",
            )
        ref_a = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref_a is None or ref_a.sandbox_id is None:
            return ScenarioResult(
                name="restart_with_fresh", passed=False,
                error="service A didn't persist a live ref",
            )
        original_sandbox_id = ref_a.sandbox_id

        # ---- Operator override: wipe ----
        t0 = time.monotonic()
        cleared = await service_a.wipe_for_fresh_restart()
        timings["wipe_ms"] = (time.monotonic() - t0) * 1000
        if cleared != 1:
            return ScenarioResult(
                name="restart_with_fresh", passed=False, timings_ms=timings,
                error=f"wipe cleared {cleared} refs, expected 1",
            )
        ref_after_wipe = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref_after_wipe is not None:
            return ScenarioResult(
                name="restart_with_fresh", passed=False, timings_ms=timings,
                error="persistence ref survived wipe_for_fresh_restart",
            )

        # ---- Service B: fresh create ----
        service_b = E2BSandboxService(
            client,
            mcpolis_instance=f"e2e-{RUN_ID}-fresh-b",
            on_timeout_seconds=IDLE_PAUSE_SECONDS,
            persistence=persistence,
            reuse_sandboxes_on_restart=True,
        )
        t1 = time.monotonic()
        async with service_b.session(
            session_id=f"e2e-{RUN_ID}-fresh-b",
            org_id=org_id,
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, LogBuffer()),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["b_fresh_open_ms"] = (time.monotonic() - t1) * 1000
            live_handle = service_b._live_sandboxes[  # type: ignore[reportPrivateUsage]
                f"e2e-{RUN_ID}-fresh-b"
            ]
            if live_handle.sandbox_id == original_sandbox_id:
                return ScenarioResult(
                    name="restart_with_fresh", passed=False,
                    timings_ms=timings,
                    error=(
                        "service B reused sandbox "
                        f"{original_sandbox_id!r} after wipe — "
                        "fresh-restart didn't take effect"
                    ),
                )
            new_sandbox_id = live_handle.sandbox_id

            session_b = ClientSession(read_stream, write_stream)
            async with session_b:
                await asyncio.wait_for(
                    session_b.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                tools = await session_b.list_tools()
                if not any(t.name == "echo" for t in tools.tools):
                    return ScenarioResult(
                        name="restart_with_fresh", passed=False,
                        timings_ms=timings,
                        error="echo tool missing on fresh sandbox",
                    )
        # Cleanup: kill the new sandbox so we don't leak.
        try:
            await client.kill_sandbox(new_sandbox_id)
        except Exception:
            pass
        return ScenarioResult(
            name="restart_with_fresh", passed=True, timings_ms=timings,
            notes=[
                f"wipe killed sandbox {original_sandbox_id} + cleared ref",
                f"service B created new sandbox {new_sandbox_id} (≠ original)",
            ],
        )
    except Exception as exc:
        # Best-effort cleanup of any leftover sandbox.
        if original_sandbox_id is not None:
            try:
                await client.kill_sandbox(original_sandbox_id)
            except Exception:
                pass
        return ScenarioResult(
            name="restart_with_fresh", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def restart_skips_wakeup() -> ScenarioResult:
    """The headline assertion for the lazy-connect path: a backend
    restart with cached metadata in the persistence ref MUST NOT
    wake the upstream's E2B sandbox.

    Flow:
      1. Manager A: ``connect_shared`` for a service_account stdio
         upstream. Opens MCP session → caches ``server_info`` /
         ``self_description`` → ``E2BSandboxService`` persists the
         live ref → ``UpstreamClientManager._persist_cached_metadata``
         folds the metadata fields into the same ref.
      2. Close manager A cleanly (skip-kill leaves the sandbox alive
         on E2B).
      3. Sleep past ``IDLE_PAUSE_SECONDS`` so E2B auto-pauses the
         sandbox.
      4. Snapshot the log-capture cursor.
      5. Build manager B against the same persistence repo and call
         ``start_all()``. With the metadata cache populated, boot
         must skip ``connect_shared`` for our stdio upstream and emit
         ``upstream.client.boot.deferred_attach`` instead.

    Asserts (since cursor):
      - **Zero** ``sandbox.e2b.create`` events (fresh creates would
        wake-equivalent).
      - **Zero** ``sandbox.e2b.reconnect.ok`` events (any reconnect
        attempt issues ``Sandbox.connect`` → auto_resume → wake).
      - **Exactly one** ``upstream.client.boot.deferred_attach`` for
        our upstream.
      - ``manager_b.get_server_info(upstream.id)`` returns the cached
        value (so the dashboard can render without a session).

    Proof of the lazy path:
      - ``ensure_shared_connected`` after the assertions opens the
        session, emits ``sandbox.e2b.reconnect.ok`` with the original
        sandbox_id (sandbox was paused, not destroyed). The wake
        happens here, on demand — not at boot. That's the goal.

    Cleanup: explicit kill at the end so the test doesn't leak.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )

    upstream = _upstream(
        f"nowake-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    client = RealE2BClient(api_key=cast(str, API_KEY))
    org_id = f"acme-{RUN_ID}"
    timings: dict[str, float] = {}

    # ---- Manager A: original boot + cache populate ----
    service_a = E2BSandboxService(
        client,
        mcpolis_instance=f"e2e-{RUN_ID}",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=persistence,
        reuse_sandboxes_on_restart=True,
    )
    manager_a = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service_a},
        sandbox_persistence=persistence,
        mcpolis_instance=f"e2e-{RUN_ID}",
    )
    original_sandbox_id: str | None = None
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(
            manager_a.connect_shared(upstream),
            timeout=INITIALIZE_TIMEOUT,
        )
        timings["a_connect_ms"] = (time.monotonic() - t0) * 1000

        ref_after_a = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref_after_a is None or ref_after_a.sandbox_id is None:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error="manager A didn't persist a live ref",
            )
        if ref_after_a.cached_server_info is None:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    "manager A didn't persist cached_server_info — "
                    "boot would have nothing to skip from"
                ),
            )
        original_sandbox_id = ref_after_a.sandbox_id

        # Manager A out of the way. Mark every active session
        # preserve-on-close so the lifespan-handler shutdown contract
        # is simulated — sandbox + cached metadata survive into
        # manager B's boot, which is the whole point of the
        # boot-skip path under test.
        service_a.mark_all_active_sessions_preserve_on_close()
        await manager_a.stop_all()

        # Wait past auto-pause so the sandbox is actually paused
        # when manager B boots — proves "no wakeup" rather than just
        # "no fresh-create".
        print(
            f"  [restart_skips_wakeup] sleeping "
            f"{REATTACH_WAIT_SECONDS}s past idle-pause window …",
            flush=True,
        )
        await asyncio.sleep(REATTACH_WAIT_SECONDS)

        # ---- Manager B: simulated restart, cursor-captured boot ----
        cursor_ns = time.monotonic_ns()
        service_b = E2BSandboxService(
            client,
            mcpolis_instance=f"e2e-{RUN_ID}-b",
            on_timeout_seconds=IDLE_PAUSE_SECONDS,
            persistence=persistence,
            reuse_sandboxes_on_restart=True,
        )
        manager_b = UpstreamClientManager(
            upstreams=[upstream],
            org_id=org_id,
            sandbox_resolver=SandboxResolver(global_provider="e2b"),
            sandbox_services={"e2b": service_b},
            sandbox_persistence=persistence,
            mcpolis_instance=f"e2e-{RUN_ID}-b",
        )
        # Drive the same helper that ``OrgRuntimeManager.connect_runtime``
        # invokes in prod — ``start_all`` is dev-stack-only, and a prior
        # version of this scenario exercising it failed to catch a bug
        # where the boot-skip gate wasn't actually wired into the prod
        # path. ``connect_shared_or_defer`` is the single source of
        # truth for the gate; both paths funnel through it.
        t1 = time.monotonic()
        deferred = await manager_b.connect_shared_or_defer(upstream)
        if not deferred:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    "connect_shared_or_defer returned False — boot-skip "
                    "gate didn't fire even though persistence carries "
                    "the cache; an eager connect was performed at boot"
                ),
            )
        timings["b_boot_ms"] = (time.monotonic() - t1) * 1000

        # Assertion 1: NO E2B create events.
        creates = _log_capture.find_events(
            "sandbox.e2b.create", since_ns=cursor_ns,
        )
        if creates:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    f"boot triggered {len(creates)} sandbox.e2b.create "
                    f"event(s) — would wake-equivalent: {creates}"
                ),
            )

        # Assertion 2: NO reconnect.ok events. Any reconnect attempt
        # calls Sandbox.connect → auto_resume → the very wake we're
        # avoiding.
        reconnects = _log_capture.find_events(
            "sandbox.e2b.reconnect.ok", since_ns=cursor_ns,
        )
        if reconnects:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    f"boot triggered {len(reconnects)} reconnect.ok "
                    f"event(s) — Sandbox.connect would wake the box"
                ),
            )

        # The "deferred_attach" log line is emitted by the callers
        # of ``connect_shared_or_defer`` (``start_all`` /
        # ``connect_runtime``), not by the helper itself. Since this
        # scenario invokes the helper directly, we already proved the
        # gate fired by checking its return value — no need to grep
        # for the caller-side log line.

        # Assertion 3: dashboard can render from cache (the whole
        # point of caching server_info on the ref).
        cached = manager_b.get_server_info(upstream.id)
        if cached is None or not cached.name:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    "get_server_info returned None / empty name "
                    "post-boot — dashboard would render blank "
                    "server identity for a deferred-attach upstream"
                ),
            )

        # Assertion 4: USER-VISIBLE READINESS. Every place the UI
        # answers "is this MCP ready?" must report True for a
        # deferred-attach upstream — otherwise admins see "Stopped"
        # for upstreams they never stopped and panic-click
        # reconnect, which wakes the sandbox we deliberately paused.
        # The prior version of this scenario was thorough about
        # wake-free mechanics but silent about user-visible state;
        # below pins every reader of that state at once so a future
        # refactor can't reintroduce a single-surface drift.
        readiness_checks: list[tuple[str, bool]] = [
            # Admin upstream readiness gate
            # (dashboard_api._readiness_for_upstream).
            ("is_connected(upstream.id)",
             manager_b.is_connected(upstream.id)),
            # Superadmin org listing + system tile + UpstreamConfigService.
            ("upstream.id in ready_upstream_ids",
             upstream.id in manager_b.ready_upstream_ids),
            # Server-info accessor — drives the upstream-detail page's
            # name/version line and the dashboard tile sub-text.
            ("get_server_info(upstream.id) is not None",
             manager_b.get_server_info(upstream.id) is not None),
            # Self-description — used for `instructions` / extensions.
            ("get_self_description(upstream.id) is not None",
             manager_b.get_self_description(upstream.id) is not None),
        ]
        failed_checks = [name for name, ok in readiness_checks if not ok]
        if failed_checks:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    f"deferred-attach upstream not surfaced as ready "
                    f"to the user via: {', '.join(failed_checks)}. "
                    f"Dashboard / superadmin / config endpoints would "
                    f"render this upstream as Stopped or Not started."
                ),
            )

        # ---- Lazy path proof: ensure_shared_connected wakes ----
        # NOW we expect a wake (the actual user demand). The
        # sandbox_id should match the original, proving the box
        # survived the pause and was reattached to (not freshly
        # created).
        cursor_lazy_ns = time.monotonic_ns()
        t2 = time.monotonic()
        await asyncio.wait_for(
            manager_b.ensure_shared_connected(upstream),
            timeout=INITIALIZE_TIMEOUT,
        )
        timings["b_lazy_connect_ms"] = (time.monotonic() - t2) * 1000

        lazy_reconnects = _log_capture.find_events(
            "sandbox.e2b.reconnect.ok", since_ns=cursor_lazy_ns,
        )
        if not lazy_reconnects:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    "ensure_shared_connected didn't emit "
                    "sandbox.e2b.reconnect.ok — the lazy path didn't "
                    "actually reattach"
                ),
            )
        lazy_sandbox_id = lazy_reconnects[0].get("sandbox_id")
        if lazy_sandbox_id != original_sandbox_id:
            return ScenarioResult(
                name="restart_skips_wakeup", passed=False,
                timings_ms=timings,
                error=(
                    f"lazy reattach went to sandbox "
                    f"{lazy_sandbox_id!r}, expected "
                    f"{original_sandbox_id!r}"
                ),
            )

        await manager_b.stop_all()

        # Cleanup: kill the survivor.
        try:
            await client.kill_sandbox(original_sandbox_id)
        except Exception:
            pass

        return ScenarioResult(
            name="restart_skips_wakeup", passed=True, timings_ms=timings,
            notes=[
                "boot emitted deferred_attach (no E2B-side calls)",
                "dashboard cache served server_info without a session",
                f"lazy reattach hit sandbox {original_sandbox_id}",
            ],
        )
    except Exception as exc:
        # Best-effort cleanup.
        if original_sandbox_id is not None:
            try:
                await client.kill_sandbox(original_sandbox_id)
            except Exception:
                pass
        return ScenarioResult(
            name="restart_skips_wakeup", passed=False,
            timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


def _build_org_runtime_for_restart(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    persistence: object,
    e2b_client: RealE2BClient,
    instance_suffix: str = "restart",
):  # type: ignore[no-untyped-def]
    """Build the OrgRuntimeManager + OrgRuntime harness used by the
    restart scenarios. Single source of truth so each scenario tests
    the *same* prod boot path (``OrgRuntimeManager.connect_runtime``)
    rather than each one drifting toward an inner helper that
    happens to work but might miss the actual bug class.

    Returns ``(org_manager, runtime, manager)`` — the caller drives
    ``org_manager.connect_runtime(runtime)`` and asserts on
    user-visible state via ``manager.is_connected`` etc.
    """
    from mcpolis.adapters.repositories.audit_repository import AuditRepository
    from mcpolis.domain.ports import ConfigRepository, ToolCatalogRepository
    from mcpolis.domain.services.org_runtime import (
        OrgRuntime,
        OrgRuntimeManager,
        StartupStatus,
    )
    from mcpolis.domain.services.policy_engine import PolicyEngine
    from mcpolis.domain.services.tool_registry import ToolRegistry
    from mcpolis.domain.model.settings import (
        RoleDefinition,
        SettingsConfig,
        UserDefinition,
    )

    service = E2BSandboxService(
        e2b_client,
        mcpolis_instance=f"e2e-{RUN_ID}-{instance_suffix}",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=cast(Any, persistence),
        reuse_sandboxes_on_restart=True,
    )
    manager = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service},
        sandbox_persistence=cast(Any, persistence),
        mcpolis_instance=f"e2e-{RUN_ID}-{instance_suffix}",
    )
    config = SettingsConfig(
        roles={"admin": RoleDefinition(is_admin=True)},
        users={"admin@example.com": UserDefinition(role="admin")},
    )
    policy_engine = PolicyEngine(config)
    tool_registry = MagicMock(spec=ToolRegistry)
    tool_registry.hydrate = AsyncMock()
    tool_registry.refresh_all = AsyncMock()
    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=policy_engine,
        tool_registry=tool_registry,
        client_manager=manager,
        tool_router=MagicMock(),
        config_service=MagicMock(),
        upstreams=[upstream],
    )
    connection_repo = MagicMock()
    connection_repo.get_disabled_ids = AsyncMock(return_value=set())
    connection_repo.set_disabled = AsyncMock()
    connection_repo.list_user_tokens = AsyncMock(return_value=[])
    org_manager = OrgRuntimeManager(
        config_repo=MagicMock(spec=ConfigRepository),
        upstream_config_repo=MagicMock(),
        connection_repo=connection_repo,
        audit_repo=MagicMock(spec=AuditRepository),
        tool_catalog_repo=MagicMock(spec=ToolCatalogRepository),
        server_url="http://localhost:8080",
    )
    org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]
    org_manager._startup_status[org_id] = StartupStatus(  # pyright: ignore[reportPrivateUsage]
        total=1,
    )
    return org_manager, runtime, manager


async def _make_cached_upstream_alive_on_e2b(
    *,
    org_id: str,
    upstream: UpstreamDefinition,
    persistence: object,
    e2b_client: RealE2BClient,
):  # type: ignore[no-untyped-def]
    """Drive a real ``connect_shared`` to put the upstream into the
    "was ready before shutdown" state: real sandbox alive on E2B,
    persistence ref carries cached_server_info + sandbox_id + pid.

    Returns the sandbox_id of the live sandbox so the caller can
    perturb / kill it before the simulated restart.
    """
    from mcpolis.domain.ports.sandbox_persistence_repository import (
        SandboxPersistedRef,
    )
    service_warmup = E2BSandboxService(
        e2b_client,
        mcpolis_instance=f"e2e-{RUN_ID}-warmup",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
        persistence=cast(Any, persistence),
        reuse_sandboxes_on_restart=True,
    )
    manager_warmup = UpstreamClientManager(
        upstreams=[upstream],
        org_id=org_id,
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service_warmup},
        sandbox_persistence=cast(Any, persistence),
        mcpolis_instance=f"e2e-{RUN_ID}-warmup",
    )
    await asyncio.wait_for(
        manager_warmup.connect_shared(upstream),
        timeout=INITIALIZE_TIMEOUT,
    )
    # Simulate the lifespan-handler shutdown contract: every active
    # session is marked preserve-on-close before tear-down so the
    # sandbox + persistence ref survive into the next boot's
    # reconnect. Without this, the kill-on-stop default fires and
    # the restart scenarios below find an empty persistence layer.
    service_warmup.mark_all_active_sessions_preserve_on_close()
    await manager_warmup.stop_all()
    persistence_obj = cast("InMemorySandboxPersistenceRepository", persistence)
    ref: SandboxPersistedRef | None = await persistence_obj.get(
        org_id=org_id, upstream_id=upstream.id,
    )
    assert ref is not None and ref.sandbox_id is not None
    assert ref.cached_server_info is not None, (
        "warmup didn't populate cached_server_info — restart "
        "scenarios won't exercise the cached path"
    )
    return ref.sandbox_id


async def restart_recovers_from_killed_sandbox() -> ScenarioResult:
    """Option-A loose semantics, sandbox-killed-externally case.
    The persisted sandbox got killed (E2B GC, account ops, kill
    from another instance) between mcpolis shutdown and the next
    user request. The "Ready" badge promises a working tool call:
    the lazy reattach must fall back to fresh-create transparently.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )

    upstream = _upstream(
        f"recover-killed-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    e2b_client = RealE2BClient(api_key=cast(str, API_KEY))
    org_id = f"acme-{RUN_ID}"
    timings: dict[str, float] = {}
    new_sandbox_id: str | None = None

    try:
        # Warmup: real sandbox + cached_server_info populated.
        t0 = time.monotonic()
        original_sandbox_id = await _make_cached_upstream_alive_on_e2b(
            org_id=org_id, upstream=upstream,
            persistence=persistence, e2b_client=e2b_client,
        )
        timings["warmup_ms"] = (time.monotonic() - t0) * 1000

        # Perturbation: kill the sandbox externally — mimics E2B
        # account-side GC / forced kill from another process.
        await e2b_client.kill_sandbox(original_sandbox_id)

        # Restart: build the boot stack against the same
        # persistence (which still says the sandbox exists).
        org_manager, runtime, manager = _build_org_runtime_for_restart(
            org_id=org_id, upstream=upstream,
            persistence=persistence, e2b_client=e2b_client,
        )

        cursor_ns = time.monotonic_ns()
        t1 = time.monotonic()
        await org_manager.connect_runtime(runtime)
        timings["connect_runtime_ms"] = (time.monotonic() - t1) * 1000

        # is_connected is the dashboard's "Ready" gate. Loose
        # semantics: True after boot for cached upstreams,
        # regardless of whether the sandbox really exists.
        if not manager.is_connected(upstream.id):
            return ScenarioResult(
                name="restart_recovers_from_killed_sandbox", passed=False,
                timings_ms=timings,
                error=(
                    "is_connected returned False after connect_runtime "
                    "for a cached upstream — the loose ('Ready' trusts "
                    "the cache) contract is broken at boot"
                ),
            )
        del cursor_ns  # boot-time E2B activity is fine here

        # Now the act: a real tool call. The lazy reattach should
        # fail (sandbox gone), kick fresh-create, MCP initialize,
        # tool call succeeds. Drive through ``connect_shared`` —
        # mirrors what tool dispatch does on first call after boot.
        t2 = time.monotonic()
        await asyncio.wait_for(
            manager.connect_shared(upstream),
            timeout=INITIALIZE_TIMEOUT,
        )
        timings["recover_ms"] = (time.monotonic() - t2) * 1000

        # Verify: the new ref points at a NEW sandbox (= we
        # actually fresh-created instead of silently failing).
        from mcpolis.domain.ports.sandbox_persistence_repository import (
            SandboxPersistedRef,
        )
        ref: SandboxPersistedRef | None = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref is None or ref.sandbox_id is None:
            return ScenarioResult(
                name="restart_recovers_from_killed_sandbox", passed=False,
                timings_ms=timings,
                error="post-recovery ref is missing or has no sandbox_id",
            )
        if ref.sandbox_id == original_sandbox_id:
            return ScenarioResult(
                name="restart_recovers_from_killed_sandbox", passed=False,
                timings_ms=timings,
                error=(
                    "ref still points at the dead sandbox after recovery — "
                    "fresh-create fallback didn't execute"
                ),
            )
        new_sandbox_id = ref.sandbox_id

        return ScenarioResult(
            name="restart_recovers_from_killed_sandbox", passed=True,
            timings_ms=timings,
            notes=[
                "is_connected=True at boot (loose mode)",
                f"recovery fresh-created {new_sandbox_id} after dead "
                f"{original_sandbox_id}",
            ],
        )
    finally:
        # Cleanup any sandbox we created during recovery.
        if new_sandbox_id is not None:
            try:
                await e2b_client.kill_sandbox(new_sandbox_id)
            except Exception:
                pass


async def restart_recovers_from_dead_mcp_process() -> ScenarioResult:
    """Option-A loose semantics, dead-MCP-process case. The
    persisted sandbox is alive on E2B but the MCP subprocess
    inside has died (crashed mid-run, OOM-killed, etc.). The
    lazy reattach hits ``connect_command_failed`` → kills the
    stuck sandbox + fresh-creates a replacement → tool call
    works.

    Simulated by corrupting the persisted pid to a value the
    sandbox doesn't have. Same observable behaviour as a real
    crash from the SDK's perspective.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )
    from mcpolis.domain.ports.sandbox_persistence_repository import (
        SandboxPersistedRef,
    )

    upstream = _upstream(
        f"recover-deadpid-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    e2b_client = RealE2BClient(api_key=cast(str, API_KEY))
    org_id = f"acme-{RUN_ID}"
    timings: dict[str, float] = {}
    new_sandbox_id: str | None = None
    original_sandbox_id: str | None = None

    try:
        t0 = time.monotonic()
        original_sandbox_id = await _make_cached_upstream_alive_on_e2b(
            org_id=org_id, upstream=upstream,
            persistence=persistence, e2b_client=e2b_client,
        )
        timings["warmup_ms"] = (time.monotonic() - t0) * 1000

        # Perturbation: corrupt the pid. The sandbox stays alive
        # on E2B; the SDK's ``commands.connect(99999)`` will
        # raise NotFoundException — same behaviour as a real
        # MCP process crash.
        ref: SandboxPersistedRef | None = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        assert ref is not None
        await persistence.upsert(ref.model_copy(update={"pid": 999_999_999}))

        org_manager, runtime, manager = _build_org_runtime_for_restart(
            org_id=org_id, upstream=upstream,
            persistence=persistence, e2b_client=e2b_client,
        )
        await org_manager.connect_runtime(runtime)

        if not manager.is_connected(upstream.id):
            return ScenarioResult(
                name="restart_recovers_from_dead_mcp_process", passed=False,
                timings_ms=timings,
                error="is_connected=False after boot — loose contract broken",
            )

        # Tool call: lazy reattach → connect_command fails → kill
        # stuck sandbox → fresh-create.
        t2 = time.monotonic()
        await asyncio.wait_for(
            manager.connect_shared(upstream),
            timeout=INITIALIZE_TIMEOUT,
        )
        timings["recover_ms"] = (time.monotonic() - t2) * 1000

        ref_after = await persistence.get(
            org_id=org_id, upstream_id=upstream.id,
        )
        if ref_after is None or ref_after.sandbox_id is None:
            return ScenarioResult(
                name="restart_recovers_from_dead_mcp_process", passed=False,
                timings_ms=timings,
                error="post-recovery ref is missing or has no sandbox_id",
            )
        if ref_after.sandbox_id == original_sandbox_id:
            return ScenarioResult(
                name="restart_recovers_from_dead_mcp_process", passed=False,
                timings_ms=timings,
                error=(
                    "ref still points at the original sandbox — the "
                    "stuck-process kill+fresh-create path didn't fire"
                ),
            )
        new_sandbox_id = ref_after.sandbox_id

        return ScenarioResult(
            name="restart_recovers_from_dead_mcp_process", passed=True,
            timings_ms=timings,
            notes=[
                "is_connected=True at boot",
                f"recovery killed {original_sandbox_id} + "
                f"fresh-created {new_sandbox_id}",
            ],
        )
    finally:
        # Cleanup both the stuck sandbox (the kill in recovery may
        # have been best-effort) and the fresh one.
        for sbx_id in (original_sandbox_id, new_sandbox_id):
            if sbx_id is not None:
                try:
                    await e2b_client.kill_sandbox(sbx_id)
                except Exception:
                    pass


async def restart_reattaches_through_stale_config() -> ScenarioResult:
    """Boot reconnect contract: a config edit on disk does NOT take
    effect on backend restart. The user must explicitly Stop+Restart
    for new args/env to apply. So when the live upstream config has
    drifted from what was used when the persisted sandbox was
    created, the reconnect must still REATTACH (not silently apply
    the pending edit by killing + fresh-creating).

    Replaces the old ``restart_recovers_from_stale_config`` which
    pinned the now-removed config_hash drift gate. Under the
    kill-on-stop contract, drift is the user's pending intent,
    not authorization to apply across a deploy.
    """
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )

    # Warmup with config A.
    upstream_v1 = _upstream(
        f"reattach-drift-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
        env={"FEATURE_FLAG": "off"},
    )
    persistence = InMemorySandboxPersistenceRepository()
    e2b_client = RealE2BClient(api_key=cast(str, API_KEY))
    org_id = f"acme-{RUN_ID}"
    timings: dict[str, float] = {}
    original_sandbox_id: str | None = None

    try:
        t0 = time.monotonic()
        original_sandbox_id = await _make_cached_upstream_alive_on_e2b(
            org_id=org_id, upstream=upstream_v1,
            persistence=persistence, e2b_client=e2b_client,
        )
        timings["warmup_ms"] = (time.monotonic() - t0) * 1000

        # Boot service B with a DIFFERENT upstream definition
        # (env changed). This mimics "admin edited the JSON
        # while the backend was down" — the persisted sandbox
        # was created with config A, the live config is B.
        upstream_v2 = _upstream(
            f"reattach-drift-{RUN_ID}",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-everything"],
            env={"FEATURE_FLAG": "on"},
        )
        org_manager, runtime, manager = _build_org_runtime_for_restart(
            org_id=org_id, upstream=upstream_v2,
            persistence=persistence, e2b_client=e2b_client,
        )
        await org_manager.connect_runtime(runtime)

        if not manager.is_connected(upstream_v2.id):
            return ScenarioResult(
                name="restart_reattaches_through_stale_config", passed=False,
                timings_ms=timings,
                error="is_connected=False after boot — loose contract broken",
            )

        # Lazy reattach: the in-memory state is DEFERRED_ATTACH
        # from boot; the next ``connect_shared`` MUST reattach to
        # the SAME sandbox, not fresh-create.
        t2 = time.monotonic()
        await asyncio.wait_for(
            manager.connect_shared(upstream_v2),
            timeout=INITIALIZE_TIMEOUT,
        )
        timings["reattach_ms"] = (time.monotonic() - t2) * 1000

        ref_after = await persistence.get(
            org_id=org_id, upstream_id=upstream_v2.id,
        )
        if ref_after is None or ref_after.sandbox_id is None:
            return ScenarioResult(
                name="restart_reattaches_through_stale_config", passed=False,
                timings_ms=timings,
                error="post-reattach ref is missing or has no sandbox_id",
            )
        if ref_after.sandbox_id != original_sandbox_id:
            return ScenarioResult(
                name="restart_reattaches_through_stale_config", passed=False,
                timings_ms=timings,
                error=(
                    f"ref points at a NEW sandbox {ref_after.sandbox_id!r} "
                    f"instead of reattaching to the original "
                    f"{original_sandbox_id!r} — drift was silently applied "
                    f"across the deploy, violating the kill-on-stop contract"
                ),
            )

        return ScenarioResult(
            name="restart_reattaches_through_stale_config", passed=True,
            timings_ms=timings,
            notes=[
                "is_connected=True at boot",
                f"reattached to original sandbox {original_sandbox_id} "
                f"despite env drift (FEATURE_FLAG: off → on); pending "
                f"edit will only take effect on user Stop+Restart",
            ],
        )
    finally:
        if original_sandbox_id is not None:
            try:
                await e2b_client.kill_sandbox(original_sandbox_id)
            except Exception:
                pass


async def restart_skips_never_ready_upstream() -> ScenarioResult:
    """Demonstrates the bug observed in prod 2026-05-01: an upstream
    that was NOT in a proper running state when mcpolis shut down
    (no ``cached_server_info`` in the persistence ref) gets its
    sandbox woken / re-created on restart.

    The user's "was_ready" rule says: don't wake or boot a sandbox
    for an upstream that wasn't successfully running at shutdown.
    The cache field is the proof signal — it only populates after a
    successful ``connect_shared`` (i.e. MCP ``initialize`` succeeded).
    No cache → never ready → boot must skip without touching E2B.

    Setup mirrors prod state of ``bogus``:
      - persistence ref present (sandbox_id + pid from a prior
        boot's fresh-create attempt).
      - ``cached_server_info`` is ``None`` (the MCP ``initialize``
        failed every time, so the cache never populated).
      - ``enabled: True`` is the equivalent of the admin's past
        Connect click (we mock it via the connection_repo).

    The sandbox we pre-create here represents the "leftover" sandbox
    from the prior boot — alive on E2B (presumably auto-paused after
    idle), referenced by the persistence ref.

    Assertions:
      - **Zero** ``sandbox.e2b.reconnect.ok`` events for our upstream
        — any reconnect attempt would call ``Sandbox.connect`` →
        auto_resume → wake.
      - **Zero** ``sandbox.e2b.create`` events for our upstream — a
        fresh-create on top of the orphan would mean we're booting
        a NEW sandbox for an upstream that never worked.
      - The pre-created leftover sandbox stays in its starting
        state (queryable via ``client.list_sandboxes``).

    Cleanup: kills the leftover sandbox at the end so the test
    doesn't leak.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, AsyncMock

    from mcpolis.adapters.repositories.audit_repository import AuditRepository
    from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
        InMemorySandboxPersistenceRepository,
    )
    from mcpolis.domain.ports import ConfigRepository, ToolCatalogRepository
    from mcpolis.domain.ports.sandbox_persistence_repository import (
        SandboxPersistedRef,
    )
    from mcpolis.domain.services.org_runtime import (
        OrgRuntime,
        OrgRuntimeManager,
        StartupStatus,
    )
    from mcpolis.domain.services.policy_engine import PolicyEngine
    from mcpolis.domain.services.tool_registry import ToolRegistry
    from mcpolis.domain.model.settings import (
        RoleDefinition,
        SettingsConfig,
        UserDefinition,
    )

    upstream = _upstream(
        f"never-ready-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    persistence = InMemorySandboxPersistenceRepository()
    e2b_client = RealE2BClient(api_key=cast(str, API_KEY))
    org_id = f"acme-{RUN_ID}"
    timings: dict[str, float] = {}
    leftover_sandbox_id: str | None = None

    try:
        # --- Pre-condition: a leftover sandbox on E2B from a prior
        # failed boot. Real ``Sandbox.create`` against the matching
        # template — we don't run a command, just spawn the box.
        t0 = time.monotonic()
        leftover_handle = await e2b_client.create_sandbox(
            template="mcpolis-node-cpu1-ram1024",
            metadata={"mcpolis_instance": f"e2e-{RUN_ID}-prior"},
            timeout_seconds=IDLE_PAUSE_SECONDS,
        )
        leftover_sandbox_id = leftover_handle.sandbox_id
        timings["leftover_create_ms"] = (time.monotonic() - t0) * 1000

        # Persistence ref mirrors what the prior boot's fresh-create
        # would have written: sandbox_id + pid (process is gone, so
        # any reconnect would fail), but NO ``cached_server_info``
        # (MCP initialize never succeeded).
        await persistence.upsert(SandboxPersistedRef(
            provider="e2b",
            org_id=org_id,
            upstream_id=upstream.id,
            mcpolis_instance=f"e2e-{RUN_ID}-prior",
            sandbox_id=leftover_sandbox_id,
            paused_snapshot_id=None,
            pid=99999,  # arbitrary; the process is gone
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime.now(UTC),
        ))

        # --- Build the boot stack: OrgRuntime + OrgRuntimeManager,
        # the same shape ``app.py`` constructs in prod.
        service = E2BSandboxService(
            e2b_client,
            mcpolis_instance=f"e2e-{RUN_ID}-restart",
            on_timeout_seconds=IDLE_PAUSE_SECONDS,
            persistence=persistence,
            reuse_sandboxes_on_restart=True,
        )
        manager = UpstreamClientManager(
            upstreams=[upstream],
            org_id=org_id,
            sandbox_resolver=SandboxResolver(global_provider="e2b"),
            sandbox_services={"e2b": service},
            sandbox_persistence=persistence,
            mcpolis_instance=f"e2e-{RUN_ID}-restart",
        )

        # Minimal SettingsConfig — admin role, one user.
        config = SettingsConfig(
            roles={"admin": RoleDefinition(is_admin=True)},
            users={"admin@example.com": UserDefinition(role="admin")},
        )
        policy_engine = PolicyEngine(config)
        tool_registry = MagicMock(spec=ToolRegistry)
        tool_registry.hydrate = AsyncMock()
        tool_registry.refresh_all = AsyncMock()
        runtime = OrgRuntime(
            org_id=org_id,
            policy_engine=policy_engine,
            tool_registry=tool_registry,
            client_manager=manager,
            tool_router=MagicMock(),
            config_service=MagicMock(),
            upstreams=[upstream],
        )

        # Connection repo: enabled=True (mirroring past admin Connect),
        # disabled_ids empty (the gate we know is wrong).
        connection_repo = MagicMock()
        connection_repo.get_disabled_ids = AsyncMock(return_value=set())
        connection_repo.set_disabled = AsyncMock()
        # OAuth phase 2 reads stored tokens — empty for this test.
        connection_repo.list_user_tokens = AsyncMock(return_value=[])

        org_manager = OrgRuntimeManager(
            config_repo=MagicMock(spec=ConfigRepository),
            upstream_config_repo=MagicMock(),
            connection_repo=connection_repo,
            audit_repo=MagicMock(spec=AuditRepository),
            tool_catalog_repo=MagicMock(spec=ToolCatalogRepository),
            server_url="http://localhost:8080",
        )
        org_manager._runtimes[org_id] = runtime  # pyright: ignore[reportPrivateUsage]
        org_manager._startup_status[org_id] = StartupStatus(  # pyright: ignore[reportPrivateUsage]
            total=1,
        )

        # --- The act: run the boot reconciler. Cursor the log
        # capture so we can assert "no E2B-side activity since
        # this point."
        cursor_ns = time.monotonic_ns()
        t1 = time.monotonic()
        await org_manager.connect_runtime(runtime)
        timings["connect_runtime_ms"] = (time.monotonic() - t1) * 1000

        # --- Assertion 1: NO Sandbox.connect / reconnect events.
        # An attempt to reconnect would issue ``Sandbox.connect``
        # against the leftover, auto-resuming it (= wake = the bug
        # the user is calling out).
        reconnects = _log_capture.find_events(
            "sandbox.e2b.reconnect.ok", since_ns=cursor_ns,
        )
        cmd_failed = _log_capture.find_events(
            "sandbox.e2b.reconnect.connect_command_failed",
            since_ns=cursor_ns,
        )
        if reconnects or cmd_failed:
            return ScenarioResult(
                name="restart_skips_never_ready_upstream", passed=False,
                timings_ms=timings,
                error=(
                    "boot reconciler attempted reconnect for an upstream "
                    "with no cached_server_info — woke the leftover "
                    "sandbox. reconnects.ok="
                    f"{len(reconnects)}, "
                    f"connect_command_failed={len(cmd_failed)}"
                ),
            )

        # --- Assertion 2: NO fresh-create. Even if reconnect didn't
        # happen, falling through to ``Sandbox.create`` would mean
        # we're booting a brand new sandbox for an upstream that
        # never worked.
        creates = _log_capture.find_events(
            "sandbox.e2b.create", since_ns=cursor_ns,
        )
        if creates:
            return ScenarioResult(
                name="restart_skips_never_ready_upstream", passed=False,
                timings_ms=timings,
                error=(
                    "boot reconciler created a fresh sandbox for an "
                    f"upstream with no cached_server_info: "
                    f"creates={len(creates)}"
                ),
            )

        # --- Assertion 3: positive — the new "skip" log fired.
        # The fix should emit ``upstream.connect.skipped.never_ready``
        # so the audit trail is visible.
        skipped = [
            ev for ev in _log_capture.find_events(
                "upstream.connect.skipped.never_ready",
                since_ns=cursor_ns,
            )
            if ev.get("upstream_id") == upstream.id
        ]
        if not skipped:
            return ScenarioResult(
                name="restart_skips_never_ready_upstream", passed=False,
                timings_ms=timings,
                error=(
                    "expected ``upstream.connect.skipped.never_ready`` "
                    f"for {upstream.id!r}, got none — the boot path "
                    "produced no audit trail for the skip"
                ),
            )

        return ScenarioResult(
            name="restart_skips_never_ready_upstream", passed=True,
            timings_ms=timings,
            notes=[
                "leftover sandbox left untouched at boot",
                "no Sandbox.connect / Sandbox.create fired",
                "skipped.never_ready event emitted",
            ],
        )
    finally:
        if leftover_sandbox_id is not None:
            try:
                await e2b_client.kill_sandbox(leftover_sandbox_id)
            except Exception:
                pass


async def double_reattach() -> ScenarioResult:
    """Two consecutive auto-pause/reattach cycles on the same
    session. Verifies that ``MCPOLIS_E2B_IDLE_PAUSE_SECONDS`` holds
    across cycles — i.e., that the post-reattach ``set_timeout``
    fix in ``E2BSandboxService._session_cm`` correctly re-applies
    our configured idle window.

    Background: E2B's ``commands.connect(pid)`` triggers an
    ``auto_resume`` that resets the sandbox's idle timeout to the
    SDK default (``default_sandbox_timeout`` = 300s as of e2b
    2.20.x), NOT the value passed to ``Sandbox.create``. Without
    re-application, the second cycle's sleep window of
    ``IDLE_PAUSE_SECONDS + 5`` seconds (35s) never reaches the new
    timeout (300s) and no second pause fires. Verified empirically
    2026-05-01 via
    ``backend/tests/integration/diagnose_double_reattach.py``.

    With the fix in place: after each reattach we call
    ``sandbox.set_timeout(IDLE_PAUSE_SECONDS)``, the timer goes
    back to 30s, and the next 35s sleep correctly provokes the
    second pause. This scenario fails (cycle 2 reattach.ok does
    not fire) if the fix regresses.
    """
    service = _make_service(on_timeout_seconds=IDLE_PAUSE_SECONDS)
    upstream = _upstream(
        f"double-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-double"
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )

                # Cycle 1
                wait1_ns = time.monotonic_ns()
                print(
                    f"  [double_reattach] cycle 1: sleeping "
                    f"{REATTACH_WAIT_SECONDS}s...",
                    flush=True,
                )
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                t1 = time.monotonic()
                await asyncio.wait_for(
                    session.list_tools(), timeout=TOOL_CALL_TIMEOUT * 2,
                )
                timings["cycle_1_ms"] = (time.monotonic() - t1) * 1000
                cycle_1_events = _log_capture.find_events(
                    "sandbox.e2b.reattach.ok", since_ns=wait1_ns,
                )
                if not cycle_1_events:
                    return ScenarioResult(
                        name="double_reattach", passed=False,
                        timings_ms=timings,
                        error="cycle 1: reattach.ok did not fire",
                    )
                wake1 = cycle_1_events[0].get("reattach_duration_ms")
                if isinstance(wake1, (int, float)):
                    timings["wake_cycle_1_ms"] = float(wake1)

                # Cycle 2 — only passes when set_timeout re-apply
                # correctly resets the idle window after reattach.
                wait2_ns = time.monotonic_ns()
                print(
                    f"  [double_reattach] cycle 2: sleeping "
                    f"{REATTACH_WAIT_SECONDS}s...",
                    flush=True,
                )
                await asyncio.sleep(REATTACH_WAIT_SECONDS)
                t2 = time.monotonic()
                await asyncio.wait_for(
                    session.list_tools(), timeout=TOOL_CALL_TIMEOUT * 2,
                )
                timings["cycle_2_ms"] = (time.monotonic() - t2) * 1000
                cycle_2_events = _log_capture.find_events(
                    "sandbox.e2b.reattach.ok", since_ns=wait2_ns,
                )
                if not cycle_2_events:
                    return ScenarioResult(
                        name="double_reattach", passed=False,
                        timings_ms=timings,
                        error=(
                            "cycle 2: reattach.ok did not fire — "
                            "post-reattach set_timeout regression "
                            "(E2B reset to 300s default, our 35s "
                            "sleep didn't reach the new deadline)"
                        ),
                    )
                wake2 = cycle_2_events[0].get("reattach_duration_ms")
                if isinstance(wake2, (int, float)):
                    timings["wake_cycle_2_ms"] = float(wake2)
        return ScenarioResult(
            name="double_reattach", passed=True, timings_ms=timings,
            notes=[
                "two consecutive auto-pause/reattach cycles passed — "
                "set_timeout re-apply preserves the cost knob",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="double_reattach", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def reattach_during_concurrent_calls() -> ScenarioResult:
    """Three tool calls fired in parallel as the FIRST traffic after
    the idle window. The pump must observe ``stream_dead`` before
    the first send_stdin and reattach exactly once — not three times,
    not zero times. Then all three calls must round-trip correctly.

    This is the worst-case race for the new pump branch: multiple
    queued messages arrive before the pump has a chance to drain
    them serially. If the reattach branch isn't idempotent within a
    single iteration, we'd see redundant ``connect_command`` calls
    or, worse, half the calls sent to a dead handle.
    """
    service = _make_service(on_timeout_seconds=IDLE_PAUSE_SECONDS)
    upstream = _upstream(
        f"reattach-concurrent-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-reattach-concurrent"
    payloads = [
        f"after-pause-A-{RUN_ID}",
        f"after-pause-B-{RUN_ID}",
        f"after-pause-C-{RUN_ID}",
    ]
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                tools = await session.list_tools()
                if not any(t.name == "echo" for t in tools.tools):
                    return ScenarioResult(
                        name="reattach_during_concurrent_calls",
                        passed=False,
                        error="server-everything no longer exposes 'echo'",
                    )

                wait_start_ns = time.monotonic_ns()
                print(
                    f"  [reattach_during_concurrent] sleeping "
                    f"{REATTACH_WAIT_SECONDS}s...",
                    flush=True,
                )
                await asyncio.sleep(REATTACH_WAIT_SECONDS)

                start = time.monotonic()
                results = await asyncio.gather(
                    session.call_tool("echo", {"message": payloads[0]}),
                    session.call_tool("echo", {"message": payloads[1]}),
                    session.call_tool("echo", {"message": payloads[2]}),
                )
                timings["three_post_pause_ms"] = (
                    (time.monotonic() - start) * 1000
                )

                # Each response must contain its own payload —
                # response correlation must hold across the
                # reattach.
                for i, (expected, result) in enumerate(zip(payloads, results)):
                    blob = " ".join(
                        getattr(c, "text", "")
                        for c in result.content
                        if getattr(c, "type", None) == "text"
                    )
                    if expected not in blob:
                        return ScenarioResult(
                            name="reattach_during_concurrent_calls",
                            passed=False, timings_ms=timings,
                            error=(
                                f"response #{i} did not contain "
                                f"{expected!r} after reattach "
                                f"(got: {blob[:120]!r})"
                            ),
                        )

                # Reattach should fire **exactly once** for the
                # whole batch — not once per concurrent call.
                reattach_events = _log_capture.find_events(
                    "sandbox.e2b.reattach.ok", since_ns=wait_start_ns,
                )
                reattach_count = len(reattach_events)
                if reattach_count == 0:
                    return ScenarioResult(
                        name="reattach_during_concurrent_calls",
                        passed=False, timings_ms=timings,
                        error="reattach.ok never fired",
                    )
                if reattach_count > 1:
                    return ScenarioResult(
                        name="reattach_during_concurrent_calls",
                        passed=False, timings_ms=timings,
                        error=(
                            f"reattach.ok fired {reattach_count}× — "
                            "expected exactly 1 for a batch of "
                            "concurrent calls"
                        ),
                    )
                wake_ms = reattach_events[0].get("reattach_duration_ms")
                if isinstance(wake_ms, (int, float)):
                    timings["wake_from_paused_ms"] = float(wake_ms)
        return ScenarioResult(
            name="reattach_during_concurrent_calls", passed=True,
            timings_ms=timings,
            notes=[
                "3 concurrent echoes after pause — all routed correctly",
                "reattach.ok fired exactly once for the batch",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="reattach_during_concurrent_calls", passed=False,
            timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def bad_api_key_auth_failed() -> ScenarioResult:
    """Bad API key → SDK raises ``AuthenticationException`` →
    ``RealE2BClient`` wraps as ``E2BAuthError`` →
    ``E2BSandboxService.map_exit`` translates to
    ``ExitReason.AUTH_FAILED``. Mocked tests can't verify the real
    SDK exception class name, so this is the only place where
    ``map_exit`` is exercised against live error shapes.
    """
    from mcpolis.adapters.sandbox_e2b.client import (
        E2BAuthError,
        E2BSDKError,
    )
    from mcpolis.domain.services.sandbox_service import ProviderExitInfo

    bad_service = E2BSandboxService(
        RealE2BClient(api_key="e2b_invalid_key_for_e2e_test"),
        mcpolis_instance=f"e2e-{RUN_ID}",
        on_timeout_seconds=IDLE_PAUSE_SECONDS,
    )
    upstream = _upstream(
        f"bad-key-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    try:
        async with bad_service.session(
            session_id=f"e2e-{RUN_ID}-bad-key",
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
        ):
            return ScenarioResult(
                name="bad_api_key_auth_failed", passed=False,
                error="session() succeeded against an invalid API key",
            )
    except E2BAuthError as exc:
        # Now verify ``map_exit`` translates this to AUTH_FAILED — the
        # admin "Disconnected reason" badge depends on this mapping.
        reason, _ = bad_service.map_exit(
            ProviderExitInfo(
                error_class=type(exc).__name__,
                raw_message=str(exc),
                exit_code=None,
            ),
        )
        if reason.name != "AUTH_FAILED":
            return ScenarioResult(
                name="bad_api_key_auth_failed", passed=False,
                error=f"map_exit returned {reason.name}, want AUTH_FAILED",
            )
        return ScenarioResult(
            name="bad_api_key_auth_failed", passed=True,
            notes=[
                f"E2BAuthError raised on session() — verified",
                f"map_exit → ExitReason.AUTH_FAILED — verified",
            ],
        )
    except E2BSDKError as exc:
        return ScenarioResult(
            name="bad_api_key_auth_failed", passed=False,
            error=(
                f"expected E2BAuthError, got {type(exc).__name__}: {exc} "
                "— either the SDK error class changed or our wrapping "
                "regressed"
            ),
        )


async def subprocess_crashes_mid_session() -> ScenarioResult:
    """MCP server that initializes successfully then exits non-zero
    on a tool call. Different code path from ``bad_command`` (which
    fails before init); this exercises the
    ``ExitReason.SUBPROCESS_EXITED`` branch in ``map_exit`` and the
    surface-on-read-stream behavior of the stdin pump.

    Implementation: spawns ``server-everything`` and asks it to call
    a tool that does ``process.exit(1)``. ``server-everything``
    exposes ``longRunningOperation`` and friends — we use the
    process-killing equivalent. If no such tool exists, fall back
    to sending a kill signal via the sandbox process.
    """
    service = _make_service()
    upstream = _upstream(
        f"crash-{RUN_ID}",
        # ``node -e "process.stdin.on('data', () => process.exit(1))"``
        # — minimal MCP-shaped behaviour: accept any stdin, exit
        # non-zero. Bypasses needing server-everything to expose a
        # crash-on-demand tool. Note: this won't pass the MCP
        # initialize handshake (no JSON-RPC reply), so the scenario
        # asserts the right kind of failure rather than a successful
        # init followed by a crash. The mid-session-after-init crash
        # path is much harder to provoke deterministically against a
        # real MCP, so we settle for "init handshake fails and the
        # error surfaces cleanly via the read stream."
        command="node",
        args=[
            "-e",
            "process.stdin.on('data', () => process.exit(1));"
            "setTimeout(() => process.exit(1), 5000);",
        ],
    )
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-crash"
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                try:
                    await asyncio.wait_for(
                        session.initialize(), timeout=20.0,
                    )
                except (asyncio.TimeoutError, Exception) as init_exc:
                    return ScenarioResult(
                        name="subprocess_crashes_mid_session", passed=True,
                        notes=[
                            f"initialize failed cleanly as expected: "
                            f"{type(init_exc).__name__}",
                            "stdin pump surfaced the error rather than "
                            "hanging — verified",
                        ],
                    )
                return ScenarioResult(
                    name="subprocess_crashes_mid_session", passed=False,
                    error=(
                        "initialize succeeded against a stdin-eating "
                        "non-MCP process — should have failed"
                    ),
                )
    except Exception as exc:
        # Outer exception (sandbox-level failure) is also acceptable
        # — the point is "the failure surfaces somewhere", not
        # "exactly this exception class".
        return ScenarioResult(
            name="subprocess_crashes_mid_session", passed=True,
            notes=[
                f"sandbox surfaced the crash: {type(exc).__name__}",
            ],
        )


async def mcpolis_driven_pause_resume() -> ScenarioResult:
    """The **other** pause/resume path — ``service.pause(session_id)``
    explicitly + reopen with ``resume_from``.

    NOTE: as of 2026-05-01 this code path is **not invoked anywhere
    in production** — ``mcpolis`` has no idle reaper and no admin
    "pause now" button. Tested here for forward-compatibility: if a
    future feature wires up explicit pause, the SDK call sequence
    (``Sandbox.pause`` → ``Sandbox.connect``) and the ``SnapshotRef``
    round-trip should already work.

    Different from the auto-pause path:
      - auto-pause is E2B-driven; the sandbox stays "live" from the
        SDK's perspective, recovery is via ``commands.connect(pid)``
        on the same handle.
      - explicit pause closes the session cleanly; recovery is via
        ``Sandbox.connect(snapshot_id)`` and a brand-new
        ``run_command`` against a fresh handle.
    """
    service = _make_service()
    upstream = _upstream(
        f"explicit-pause-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    timings: dict[str, float] = {}
    snapshot_ref = None
    try:
        # Phase 1: open + initialize + pause.
        session_id_one = f"e2e-{RUN_ID}-pause-1"
        async with service.session(
            session_id=session_id_one,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                t0 = time.monotonic()
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                timings["initialize_ms"] = (time.monotonic() - t0) * 1000

            t1 = time.monotonic()
            snapshot_ref = await service.pause(session_id_one)
            timings["pause_ms"] = (time.monotonic() - t1) * 1000
            if snapshot_ref is None:
                return ScenarioResult(
                    name="mcpolis_driven_pause_resume", passed=False,
                    timings_ms=timings,
                    error="service.pause returned None for a live session",
                )
            if snapshot_ref.provider != "e2b":
                return ScenarioResult(
                    name="mcpolis_driven_pause_resume", passed=False,
                    timings_ms=timings,
                    error=f"snapshot provider={snapshot_ref.provider!r}",
                )

        # Phase 2: reopen with resume_from + drive a tool call.
        # The tool call is the part the existing real-SDK smoke
        # doesn't cover — it just verifies the session opens.
        session_id_two = f"e2e-{RUN_ID}-pause-2"
        t2 = time.monotonic()
        async with service.session(
            session_id=session_id_two,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            resume_from=snapshot_ref,
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            timings["resume_open_ms"] = (time.monotonic() - t2) * 1000
            session = ClientSession(read_stream, write_stream)
            async with session:
                # ``connect_sandbox`` brings back the sandbox but
                # NOT the MCP process — fresh ``run_command`` starts
                # a new subprocess inside, so ``initialize`` runs
                # again. Timing this separately surfaces resume cost.
                t3 = time.monotonic()
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                timings["resume_initialize_ms"] = (
                    (time.monotonic() - t3) * 1000
                )
                tools = await session.list_tools()
                echo_tool = next(
                    (t for t in tools.tools if t.name == "echo"), None,
                )
                if echo_tool is not None:
                    payload = f"resumed-{RUN_ID}"
                    result = await asyncio.wait_for(
                        session.call_tool("echo", {"message": payload}),
                        timeout=TOOL_CALL_TIMEOUT,
                    )
                    blob = " ".join(
                        getattr(c, "text", "")
                        for c in result.content
                        if getattr(c, "type", None) == "text"
                    )
                    if payload not in blob:
                        return ScenarioResult(
                            name="mcpolis_driven_pause_resume",
                            passed=False, timings_ms=timings,
                            error=(
                                f"tool call after resume returned "
                                f"unexpected payload: {blob[:120]!r}"
                            ),
                        )
        return ScenarioResult(
            name="mcpolis_driven_pause_resume", passed=True,
            timings_ms=timings,
            notes=[
                f"snapshot_id captured + round-tripped",
                f"resume → fresh init → tool call payload verified",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="mcpolis_driven_pause_resume", passed=False,
            timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


async def bad_command() -> ScenarioResult:
    """Wrong package name → ``initialize`` should time out (npm
    install fails inside the sandbox) and the session must not
    silently report Connected. Uses a short init timeout so the
    scenario doesn't sit waiting on the full 120s budget."""
    service = _make_service()
    upstream = _upstream(
        f"bad-cmd-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/this-package-does-not-exist-zzz"],
    )
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-bad"
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                # Short timeout — failure is the expected outcome.
                try:
                    await asyncio.wait_for(
                        session.initialize(), timeout=45.0,
                    )
                except (asyncio.TimeoutError, Exception) as init_exc:
                    captured = errlog.get_output()
                    return ScenarioResult(
                        name="bad_command", passed=True,
                        notes=[
                            f"initialize failed as expected: {type(init_exc).__name__}",
                            f"stderr captured: {len(captured)} bytes",
                        ],
                    )
                return ScenarioResult(
                    name="bad_command", passed=False,
                    error="initialize succeeded against a bogus package",
                )
    except Exception as exc:
        return ScenarioResult(
            name="bad_command", passed=False,
            error=f"unexpected outer exception: {type(exc).__name__}: {exc}",
        )


async def concurrent_calls() -> ScenarioResult:
    """Three ``call_tool`` requests in flight at once, **with
    distinct argument payloads** and content-based response
    verification.

    Each MCP JSON-RPC request gets a unique id; the demux on
    read_stream must deliver responses to the right caller. The
    previous version only counted responses, which would still pass
    if responses were swapped. By passing three distinguishable
    messages to ``echo`` and asserting each result contains its
    corresponding payload, response-misrouting now fails the test.
    """
    service = _make_service()
    upstream = _upstream(
        f"concurrent-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    timings: dict[str, float] = {}
    errlog = LogBuffer()
    session_id = f"e2e-{RUN_ID}-concurrent"
    payloads = [f"e2e-msg-A-{RUN_ID}", f"e2e-msg-B-{RUN_ID}", f"e2e-msg-C-{RUN_ID}"]
    try:
        async with service.session(
            session_id=session_id,
            org_id=f"acme-{RUN_ID}",
            upstream=upstream,
            resources=_resources(),
            denylist=(),
            errlog=cast(StringIO, errlog),
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                tools = await session.list_tools()
                # ``server-everything`` exposes ``echo(message)`` —
                # the canonical round-trip tool. Falling back to a
                # zero-arg tool and skipping the content check would
                # silently weaken the test, so we hard-fail instead.
                echo_tool = next(
                    (t for t in tools.tools if t.name == "echo"), None,
                )
                if echo_tool is None:
                    return ScenarioResult(
                        name="concurrent_calls", passed=False,
                        timings_ms=timings,
                        error=(
                            "server-everything no longer exposes 'echo' "
                            "— pick a different round-trip tool"
                        ),
                    )
                start = time.monotonic()
                results = await asyncio.gather(
                    session.call_tool("echo", {"message": payloads[0]}),
                    session.call_tool("echo", {"message": payloads[1]}),
                    session.call_tool("echo", {"message": payloads[2]}),
                )
                timings["three_parallel_ms"] = (time.monotonic() - start) * 1000
                # Verify each response contains its own payload
                # (not the other callers'). Response correlation
                # bug → at least one mismatch.
                for i, (expected, result) in enumerate(zip(payloads, results)):
                    blob = " ".join(
                        getattr(c, "text", "")
                        for c in result.content
                        if getattr(c, "type", None) == "text"
                    )
                    if expected not in blob:
                        return ScenarioResult(
                            name="concurrent_calls", passed=False,
                            timings_ms=timings,
                            error=(
                                f"response #{i} did not contain "
                                f"{expected!r} — JSON-RPC id demux "
                                f"likely misrouted (got: {blob[:120]!r})"
                            ),
                        )
        return ScenarioResult(
            name="concurrent_calls", passed=True, timings_ms=timings,
            notes=[
                "3 echoes with distinct payloads — all routed to the "
                "correct caller",
            ],
        )
    except Exception as exc:
        return ScenarioResult(
            name="concurrent_calls", passed=False, timings_ms=timings,
            error=f"{type(exc).__name__}: {exc}",
        )


def _make_ucm(
    *, upstream: UpstreamDefinition, service: E2BSandboxService,
) -> UpstreamClientManager:
    """Build an :class:`UpstreamClientManager` wired to a real E2B
    service — the same shape ``_build_sandbox_provider_plumbing`` in
    app.py constructs for production. Sharing this factory means
    every scenario that needs UCM goes through the prod constructor
    contract; if that signature changes, the integration suite
    breaks loudly rather than silently drifting.
    """
    return UpstreamClientManager(
        upstreams=[upstream],
        org_id=f"acme-{RUN_ID}",
        sandbox_resolver=SandboxResolver(global_provider="e2b"),
        sandbox_services={"e2b": service},
        mcpolis_instance=f"e2e-{RUN_ID}",
    )


async def status_reflects_connection() -> ScenarioResult:
    """Verifies what the dashboard "Connected" badge actually
    renders, by going through the full :class:`UpstreamClientManager`
    path the production admin route reads from.

    The route at ``GET /api/admin/upstreams/{id}`` ultimately calls
    ``client_manager.is_connected(upstream_id)`` — which under the
    state-machine refactor returns True iff the upstream's record is
    in LIVE or DEFERRED_ATTACH. Transitions to LIVE happen inside
    ``connect_shared`` only after ``SandboxConnectionTask.start()``
    awaits MCP ``initialize()``. So we drive ``connect_shared`` and
    assert the predicate flips False → True → False across the
    lifecycle. This catches drift in any of the layers in between
    (the bare ``service._live_sandboxes`` check the previous version
    of this scenario used would have missed e.g. a UCM-side bug
    where the session is registered but ``is_connected`` wasn't
    updated).
    """
    service = _make_service()
    upstream = _upstream(
        f"status-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    manager = _make_ucm(upstream=upstream, service=service)
    if manager.is_connected(upstream.id):
        return ScenarioResult(
            name="status_reflects_connection", passed=False,
            error="is_connected was True before connect_shared",
        )
    notes: list[str] = ["is_connected: False (pre-connect) — verified"]
    try:
        await asyncio.wait_for(
            manager.connect_shared(upstream), timeout=INITIALIZE_TIMEOUT,
        )
        if not manager.is_connected(upstream.id):
            return ScenarioResult(
                name="status_reflects_connection", passed=False,
                error=(
                    "is_connected was False after a successful "
                    "connect_shared — UI would render Disconnected "
                    "for an actually-running upstream"
                ),
            )
        notes.append("is_connected: True (post-connect) — UI = Connected")

        # The dashboard upstream-detail page renders
        # ``server_info.name`` / ``server_info.version`` next to
        # the badge. UCM populates ``_server_info[upstream_id]``
        # from ``task.server_info`` (client_manager.py:368-369);
        # missing or empty here ⇒ the upstream-detail page would
        # render with blank server identity even though the
        # session is alive.
        server_info = manager.get_server_info(upstream.id)
        if server_info is None or not server_info.name:
            return ScenarioResult(
                name="status_reflects_connection", passed=False,
                error=(
                    "get_server_info returned None / empty name "
                    "post-connect — UI upstream-detail page would "
                    "show blank server identity"
                ),
            )
        notes.append(
            f"server_info: name={server_info.name!r} — populated"
        )
    except Exception as exc:
        try:
            await manager.stop_all()
        except Exception:
            pass
        return ScenarioResult(
            name="status_reflects_connection", passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        await manager.stop_all()
    except Exception as exc:
        return ScenarioResult(
            name="status_reflects_connection", passed=False,
            error=f"stop_all raised: {type(exc).__name__}: {exc}",
        )
    if manager.is_connected(upstream.id):
        return ScenarioResult(
            name="status_reflects_connection", passed=False,
            error="is_connected stayed True after stop_all",
        )
    notes.append("is_connected: False (post-stop_all) — UI = Disconnected")
    return ScenarioResult(
        name="status_reflects_connection", passed=True, notes=notes,
    )


async def sse_log_stream() -> ScenarioResult:
    """End-to-end verification of the dashboard live-log SSE pipeline.

    The route at ``GET /api/admin/upstreams/{id}/logs/stream`` does
    two things:

      1. Looks up the buffer via
         ``client_manager.get_log_buffer(upstream_id)``.
      2. Iterates ``buffer.subscribe()`` and wraps each chunk in
         a ``data: ...\\n\\n`` SSE frame.

    We exercise step 1 by going through ``UpstreamClientManager``
    (the same getter the route calls) instead of constructing a
    bare ``LogBuffer`` ourselves. That way a regression where UCM
    forgets to register the buffer for an upstream — silently
    breaking the live-log panel for users — actually fails the
    test. Step 2 we exercise directly: iterating ``subscribe()`` is
    byte-for-byte what the route does inside its async generator.
    """
    service = _make_service()
    upstream = _upstream(
        f"sse-{RUN_ID}",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )
    manager = _make_ucm(upstream=upstream, service=service)

    # Two parallel subscribers — proves the buffer fan-out works.
    # Production: every dashboard tab tailing logs adds one. If
    # ``subscribe()`` only delivered to one, opening a second tab
    # would silently break the first. Each subscriber must see all
    # chunks.
    chunks_a: list[str] = []
    chunks_b: list[str] = []
    chunks_late: list[str] = []
    consumer_a: asyncio.Task[None] | None = None
    consumer_b: asyncio.Task[None] | None = None
    consumer_late: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(
            manager.connect_shared(upstream), timeout=INITIALIZE_TIMEOUT,
        )
        # connect_shared awaits initialize() — by now the buffer
        # already holds the cold-install chatter that was written
        # while npx was running. A subscriber attaching now will
        # get that chatter via the **replay** branch of subscribe()
        # (log_buffer.py:115 docstring).
        log_buffer = manager.get_log_buffer(upstream.id)
        if log_buffer is None:
            return ScenarioResult(
                name="sse_log_stream", passed=False,
                error=(
                    "client_manager.get_log_buffer returned None for "
                    "an upstream that just connected — the SSE route "
                    "would 404 in this state"
                ),
            )

        # ``LogBuffer.subscribe()`` (log_buffer.py:114-128) yields the
        # current buffered contents as a SINGLE chunk first, then
        # waits for new writes. By the time ``connect_shared``
        # returns, ``initialize()`` has already completed and no
        # further stderr is being written — so each subscriber gets
        # exactly the one replay chunk and then nothing. Capping at
        # 1 chunk matches that reality. Multi-chunk fan-out is
        # implicitly verified: both subscribers must receive the
        # same replay or the buffer's broadcaster is broken.
        async def drain(target: list[str], cap: int = 1) -> None:
            async for chunk in log_buffer.subscribe():
                if chunk:
                    target.append(chunk)
                if len(target) >= cap:
                    return

        # Two subscribers attached in parallel. Both should see the
        # same replay of pre-existing chunks plus any subsequent
        # chatter — the buffer is a fan-out broadcaster.
        consumer_a = asyncio.create_task(drain(chunks_a))
        consumer_b = asyncio.create_task(drain(chunks_b))

        # Drive a couple of MCP requests to push fresh log lines
        # through (live phase, distinct from replay).
        # ``manager.connect_shared`` already produced cold-install
        # output for the replay branch.
        await asyncio.wait_for(
            asyncio.gather(consumer_a, consumer_b), timeout=8.0,
        )

        # **Late subscriber**: attach AFTER chunks already exist.
        # Production: a user opens the live-log panel mid-session.
        # ``subscribe()`` must replay buffered history, not just
        # deliver new chunks. The buffer is a ring; if the replay
        # branch is broken, this consumer hits the timeout below.
        consumer_late = asyncio.create_task(drain(chunks_late))
        try:
            await asyncio.wait_for(consumer_late, timeout=3.0)
        except asyncio.TimeoutError:
            pass

    except Exception as exc:
        for t in (consumer_a, consumer_b, consumer_late):
            if t is not None and not t.done():
                t.cancel()
        try:
            await manager.stop_all()
        except Exception:
            pass
        return ScenarioResult(
            name="sse_log_stream", passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    for t in (consumer_a, consumer_b, consumer_late):
        if not t.done():
            t.cancel()
    try:
        await manager.stop_all()
    except Exception as exc:
        return ScenarioResult(
            name="sse_log_stream", passed=False,
            error=f"stop_all raised: {type(exc).__name__}: {exc}",
        )

    if not chunks_a or not chunks_b:
        return ScenarioResult(
            name="sse_log_stream", passed=False,
            error=(
                f"buffer fan-out failed: subscriber A got "
                f"{len(chunks_a)} chunks, B got {len(chunks_b)} — "
                "both should see the same stream"
            ),
        )
    if not chunks_late:
        return ScenarioResult(
            name="sse_log_stream", passed=False,
            error=(
                "late subscriber got 0 chunks — replay branch "
                "appears broken; opening the live-log panel "
                "mid-session would render empty"
            ),
        )
    return ScenarioResult(
        name="sse_log_stream", passed=True,
        notes=[
            f"get_log_buffer returned a registered buffer (UCM-owned)",
            f"subscriber A: {len(chunks_a)} chunks",
            f"subscriber B: {len(chunks_b)} chunks",
            f"late subscriber (replay): {len(chunks_late)} chunks",
        ],
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


SCENARIOS: list[tuple[str, Scenario]] = [
    # Fast scenarios first so failures surface within seconds.
    ("bad_api_key_auth_failed", bad_api_key_auth_failed),
    ("smoke_npx", smoke_npx),
    ("smoke_uvx", smoke_uvx),
    ("status_reflects_connection", status_reflects_connection),
    ("sse_log_stream", sse_log_stream),
    ("concurrent_calls", concurrent_calls),
    ("subprocess_crashes_mid_session", subprocess_crashes_mid_session),
    ("bad_command", bad_command),
    ("mcpolis_driven_pause_resume", mcpolis_driven_pause_resume),
    # Reattach scenarios last — each sleeps past the idle window
    # (30s+); back-to-back they dominate wall clock. Running them
    # at the end means a fast failure in earlier scenarios doesn't
    # cost the operator the reattach budget.
    ("reattach_after_idle_pause", reattach_after_idle_pause),
    ("reattach_via_ucm", reattach_via_ucm),
    ("reattach_during_concurrent_calls", reattach_during_concurrent_calls),
    # Stop/start scenarios (reuse + fresh) — exercise the
    # reuse-on-restart code path (`E2BSandboxService` with
    # ``reuse_sandboxes_on_restart=True``) and its
    # ``wipe_for_fresh_restart`` operator-override counterpart.
    ("restart_with_reuse", restart_with_reuse),
    ("restart_with_fresh", restart_with_fresh),
    # The lazy-connect scenario sleeps past the auto-pause window
    # (30s+) so the "no wake" claim is testable against a genuinely
    # paused sandbox, not just a freshly-closed one. Slot it next to
    # the other restart scenarios.
    ("restart_skips_wakeup", restart_skips_wakeup),
    # Pins the actual was_ready rule: an upstream that wasn't ready
    # before shutdown (no cached_server_info) must NOT have its
    # leftover sandbox woken / re-created on the next boot. Demoes
    # the bug observed in prod 2026-05-01 with the bogus pattern.
    ("restart_skips_never_ready_upstream", restart_skips_never_ready_upstream),
    # Option-A loose semantics: when "Ready" is true at boot for
    # a cached upstream, the first user request must succeed even
    # if the underlying E2B-side state diverged since shutdown.
    # Three perturbations that all promise the same recovery
    # contract: kill+fresh-create transparently, tool call works.
    ("restart_recovers_from_killed_sandbox", restart_recovers_from_killed_sandbox),
    ("restart_recovers_from_dead_mcp_process", restart_recovers_from_dead_mcp_process),
    # The "stale-config" scenario inverts under kill-on-stop: drift
    # is now ignored on boot reconnect (must reattach), not killed +
    # fresh-created. Renamed accordingly.
    ("restart_reattaches_through_stale_config", restart_reattaches_through_stale_config),
    # ``double_reattach`` is the slowest single scenario (~75s of
    # sleep alone). Goes last.
    ("double_reattach", double_reattach),
]


def _format_table(results: list[ScenarioResult]) -> str:
    """Single fixed-width table; readable in any terminal without
    pulling in a dep. Columns: status, name, key timings, notes."""
    rows = [("status", "scenario", "key timings (ms)", "notes")]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        timings = ", ".join(
            f"{k}={int(v)}" for k, v in sorted(r.timings_ms.items())
        ) or "-"
        notes = "; ".join(r.notes) if r.notes else (r.error or "")
        rows.append((status, r.name, timings, notes))
    widths = [
        max(len(r[i]) for r in rows) for i in range(4)
    ]
    out: list[str] = []
    for i, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))
        out.append(line)
        if i == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def _suppress_asyncgen_cleanup_noise(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    """Default exception handler swap that drops a single class of
    cosmetic stderr noise:

        ``RuntimeError: aclose(): asynchronous generator is already
        running`` (or ``athrow():``) — emitted when a vendor SDK
        leaves nested async generators (e2b_connect → httpx stream)
        for GC to clean up after we've already driven a controlled
        close via ``release()``. The first close completes
        successfully; the second one (from GC) hits the same gen
        mid-cleanup and reports an error to the loop's exception
        handler.

    The functional cleanup has already happened — these are post-hoc
    "I tried to close something already being closed" warnings, not
    correctness bugs. Suppressing them keeps the integration log
    readable (no traceback between the last scenario's PASS and the
    results table) without hiding genuine errors: any exception that
    isn't this exact pattern still goes to the default handler.
    """
    msg = context.get("message", "") or ""
    exc = context.get("exception")
    exc_msg = str(exc) if exc is not None else ""
    if (
        "asynchronous generator is already running" in msg
        or "asynchronous generator is already running" in exc_msg
        or msg.startswith("an error occurred during closing of asynchronous generator")
    ):
        return
    loop.default_exception_handler(context)


async def main() -> int:
    if not API_KEY:
        print(
            "ERROR: MCPOLIS_E2B_API_KEY (or E2B_API_KEY) must be set.",
            file=sys.stderr,
        )
        return 2

    asyncio.get_running_loop().set_exception_handler(
        _suppress_asyncgen_cleanup_noise,
    )

    # ``--only NAME[,NAME...]`` filters which scenarios run. Lets
    # operators run a single failing scenario in isolation
    # (~30s) without paying the full ~3-5 min suite.
    only: set[str] | None = None
    args = list(sys.argv[1:])
    while args:
        arg = args.pop(0)
        if arg == "--only":
            if not args:
                print("ERROR: --only needs an argument", file=sys.stderr)
                return 2
            only = set(args.pop(0).split(","))
        elif arg.startswith("--only="):
            only = set(arg.removeprefix("--only=").split(","))
        else:
            print(f"ERROR: unknown arg {arg!r}", file=sys.stderr)
            return 2

    selected = (
        [(name, scn) for (name, scn) in SCENARIOS if name in only]
        if only is not None else list(SCENARIOS)
    )
    if only is not None and len(selected) != len(only):
        unknown = only - {name for name, _ in SCENARIOS}
        if unknown:
            print(
                f"ERROR: unknown scenarios: {sorted(unknown)}",
                file=sys.stderr,
            )
            return 2

    print(f"# E2B real-SDK integration run {RUN_ID}")
    print(f"# idle pause = {IDLE_PAUSE_SECONDS}s, scenarios = {len(selected)}")
    print()

    results: list[ScenarioResult] = []
    overall_start = time.monotonic()
    for name, scenario in selected:
        print(f"[run] {name}...", flush=True)
        scenario_start = time.monotonic()
        try:
            r = await scenario()
        except Exception:
            tb = traceback.format_exc()
            r = ScenarioResult(
                name=name, passed=False,
                error=f"unhandled exception:\n{tb}",
            )
        elapsed_s = time.monotonic() - scenario_start
        print(
            f"  -> {'PASS' if r.passed else 'FAIL'} "
            f"({elapsed_s:.1f}s)",
            flush=True,
        )
        if not r.passed and r.error:
            print(f"     {r.error}", flush=True)
        results.append(r)

    overall_elapsed = time.monotonic() - overall_start
    print()
    print(f"# results — total {overall_elapsed:.1f}s")
    print()
    print(_format_table(results))
    print()

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} scenario(s) failed")
        return 1
    print(f"PASS — {len(results)}/{len(results)} scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
