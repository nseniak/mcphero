"""Graceful drain coordinator for horizontal scaling, and the shutdown.

When SIGTERM is received the backend enters *draining* mode:

1. ``/healthz`` returns ``{"status": "draining"}`` so the load balancer
   stops routing new sessions to this instance.
2. In-flight requests continue to run for up to ``drain_timeout`` seconds.
3. New requests receive a 503 Service Unavailable, in the shape their
   caller reads (``DrainMiddleware``); the dashboard's live streams are
   still let through.
4. Once all in-flight requests finish (or the timeout expires), the
   signal is handed to the server (uvicorn), which stops accepting
   connections, gives open ones ``graceful_shutdown_timeout`` seconds,
   and runs the lifespan teardown (``shut_down``).

In standalone mode the drain coordinator still works — it just means a
clean ``kill <pid>`` instead of an abrupt ``SIGKILL``.
"""
from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Any

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcpolis.adapters.upstream_clients.connection_task_base import (
    ABANDON_TIMEOUT,
)
from mcpolis.domain.services.background_tasks import (
    BackgroundTaskSet,
    drain_every_set,
    refusing_new_jobs,
)
from mcpolis.domain.services.tool_router import AUDIT_WRITE_TIMEOUT_SECONDS
from mcpolis.entrypoints.controllers.admin_tool_calls import (
    WAIT_AFTER_CANCEL_SECONDS,
)
from mcpolis.entrypoints.middleware.rate_limit_middleware import (
    is_event_stream_path,
    refusal_for,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# A shutdown step still running when its wait is over, held until it
# ends, so the job drain waits for it before the stores close.
_left_to_the_job_drain = BackgroundTaskSet()

# What a request refused while draining is told to wait: the new
# instance is usually up by then.
DRAIN_RETRY_AFTER_SECONDS = 5

# A ``signal.signal`` handler, such as uvicorn's ``Server.handle_exit``.
SignalHandler = Callable[[int, FrameType | None], object]

# Health checks bypass the drain so a load balancer can query status.
_HEALTH_PATHS = frozenset({"/healthz", "/health"})


class DrainCoordinator:
    """Tracks active requests and coordinates graceful shutdown."""

    def __init__(
        self, drain_timeout: float = 30.0, quiet_seconds: float = 1.0,
    ) -> None:
        self._drain_timeout = drain_timeout
        self._quiet_seconds = quiet_seconds
        self._draining = False
        self._active_requests = 0
        self._started_total = 0
        self._idle: asyncio.Event = asyncio.Event()
        self._idle.set()

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def request_started(self) -> None:
        self._active_requests += 1
        self._started_total += 1
        self._idle.clear()

    def request_finished(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        if self._active_requests == 0:
            self._idle.set()

    async def drain(self) -> None:
        """Enter draining mode and wait for in-flight requests to finish.

        Returns immediately if there are no active requests. Otherwise
        waits until none is active AND none started for ``quiet_seconds``
        (a client finishing an MCP tool call sends a follow-up at once,
        which the shutdown would otherwise cut), up to ``drain_timeout``.
        """
        self._draining = True
        logger.info(
            "lifecycle.drain.started",
            active_requests=self._active_requests,
            drain_timeout_seconds=self._drain_timeout,
        )

        if self._active_requests == 0:
            logger.info("lifecycle.drain.complete_immediately")
            return

        try:
            async with asyncio.timeout(self._drain_timeout):
                while True:
                    await self._idle.wait()
                    started_before = self._started_total
                    await asyncio.sleep(self._quiet_seconds)
                    if (
                        self._idle.is_set()
                        and self._started_total == started_before
                    ):
                        break
            logger.info("lifecycle.drain.complete")
        except TimeoutError:
            logger.warning(
                "lifecycle.drain.timeout",
                active_requests=self._active_requests,
            )


async def drain_then_exit(
    drain: DrainCoordinator, server_exit: SignalHandler | None,
) -> None:
    """Drain, then hand SIGTERM to the server's own handler so it shuts
    down. ``None`` (no server handler found) leaves the process draining."""
    await drain.drain()
    if server_exit is None:
        logger.warning("lifecycle.sigterm.no_server_handler")
        return
    logger.info("lifecycle.sigterm.server_exit")
    server_exit(signal.SIGTERM, None)


def install_sigterm_drain(
    drain: DrainCoordinator, tasks: BackgroundTaskSet,
) -> None:
    """Make SIGTERM drain first, then stop the server.

    Call from inside the server's lifespan. uvicorn installs its SIGTERM
    handler with ``signal.signal`` before the lifespan starts, and
    ``loop.add_signal_handler`` replaces it: without the hand-over in
    ``drain_then_exit`` the server never learns about the signal, keeps
    answering 503 until it is killed, and the lifespan teardown never
    runs (what happened from 2026-04-12 to 2026-10-07).
    """
    previous = signal.getsignal(signal.SIGTERM)
    # Only uvicorn's own handler: a handler some other code installed
    # (asyncio's no-op from an earlier add_signal_handler) would make
    # the hand-over do nothing while logging that it happened.
    server_exit = (
        previous
        if isinstance(getattr(previous, "__self__", None), uvicorn.Server)
        and callable(previous)
        else None
    )

    def _on_sigterm() -> None:
        logger.info("app.sigterm.drain_started")
        tasks.spawn(drain_then_exit(drain, server_exit))

    try:
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, _on_sigterm,
        )
    except (NotImplementedError, RuntimeError):
        # No signal support on Windows; a loop outside the main thread
        # (a test client's) cannot take signals.
        pass


def _is_event_stream(scope: Scope) -> bool:
    """A GET asking for ``text/event-stream``: the dashboard's live
    streams and the MCP notification stream. They stay open for the whole
    session, so the drain must not wait for them; the server's shutdown
    ends them."""
    if scope.get("method") != "GET":
        return False
    for name, value in scope.get("headers", []):
        if name == b"accept" and b"text/event-stream" in value:
            return True
    return False


def _continues_mcp_session(scope: Scope) -> bool:
    """A request inside an MCP session that is already open. While
    draining it is still served (and counted): a client finishing a tool
    call often sends a follow-up (the SDK re-lists tools to check the
    result), and refusing it fails the call it was waiting for. Only new
    sessions are refused."""
    return any(name == b"mcp-session-id" for name, _ in scope.get("headers", []))


def _drain_refusal(scope: Scope) -> ASGIApp:
    """503 for a request refused while draining, in the shape its caller
    reads (``refusal_for``): MCP OAuth endpoints get the OAuth code
    ``temporarily_unavailable``, so an MCP client keeps its saved sign-in
    (a plain 503 made the TypeScript SDK drop a refused token refresh and
    restart the interactive sign-in)."""
    message = "MCP Hero is restarting. Try again in a few seconds."
    return refusal_for(
        scope,
        status_code=503,
        retry_after_seconds=DRAIN_RETRY_AFTER_SECONDS,
        message=message,
        oauth_error="temporarily_unavailable",
        api_body={"detail": "Server is shutting down"},
    )


class DrainMiddleware:
    """Refuses new requests while draining and counts in-flight ones.

    A request counts until its LAST body chunk is sent, not when its
    headers go out: an MCP tool call answers with a streamed response
    whose result comes at the end, and the drain must wait for it.
    (``@app.middleware("http")`` counted only until the headers, so the
    drain never waited for a tool call.)

    The dashboard's live streams (``is_event_stream_path``) are never
    refused: a browser never retries an EventSource that got a non-200
    answer, so the tab froze until reloaded. Admitted, the stream is cut
    by the shutdown, and the browser reconnects to the new instance.

    Sits inside the CORS middleware, so a refusal carries the CORS
    headers a browser-based MCP client needs to read it.
    """

    def __init__(self, app: ASGIApp, drain: DrainCoordinator) -> None:
        self._app = app
        self._drain = drain

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _HEALTH_PATHS:
            await self._app(scope, receive, send)
            return
        if (
            self._drain.is_draining
            and not _continues_mcp_session(scope)
            and not is_event_stream_path(scope["path"])
        ):
            await _drain_refusal(scope)(scope, receive, send)
            return
        if _is_event_stream(scope):
            await self._app(scope, receive, send)
            return

        finished = False

        def finish() -> None:
            nonlocal finished
            if not finished:
                finished = True
                self._drain.request_finished()

        async def send_and_track(message: Message) -> None:
            await send(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                finish()

        self._drain.request_started()
        try:
            await self._app(scope, receive, send_and_track)
        finally:
            finish()


class ShutdownBudget(BaseModel):
    """At most how many seconds each step of ``shut_down`` waits.
    ``worst_case_seconds`` adds them up: with the request drain
    (``drain_timeout``) and uvicorn's ``graceful_shutdown_timeout`` before
    it, it must fit in the container's ``stop_grace_period``
    (docker-compose.yml) with time to spare, which
    ``tests/unit/test_container_timing.py`` checks. Usually the whole
    shutdown takes well under a second."""

    # The periodic loops and event listeners, once cancelled.
    loops: float = 2.0
    # Every MCP endpoint's sessions, closed all at once: long enough for a
    # cancelled gateway tool call to write its audit row (bounded at
    # ``AUDIT_WRITE_TIMEOUT_SECONDS``), and for a cancelled Admin MCP or
    # operator MCP call to wait for its action (``WAIT_AFTER_CANCEL_SECONDS``).
    # An action still running then goes on as a background job, waited
    # for in the next step. Shorter, every such call made this step log
    # ``app.shutdown.step_timed_out``.
    mcp_sessions: float = (
        max(AUDIT_WRITE_TIMEOUT_SECONDS, WAIT_AFTER_CANCEL_SECONDS) + 1.0
    )
    # Every background job (``drain_every_set``), the org runtimes'
    # teardown among them. At least ``ABANDON_TIMEOUT``: a connect that
    # teardown aborts waits that long for the sandbox it was creating,
    # which must record the sandbox it keeps before the stores close.
    # That also covers the 15 s a sign-in warning's SMTP step can take.
    background_jobs: float = ABANDON_TIMEOUT
    # The jobs cancelled when that wait ran out, to wind down.
    unwind: float = 2.0
    # Gateway sign-in changes not yet in storage.
    gateway_flush: float = 5.0
    # The event stream, rate limiter, lock and Mongo, closed.
    stores: float = 2.0

    def worst_case_seconds(self) -> float:
        """How long ``shut_down`` takes, after the request drain, when
        every step waits its whole budget."""
        return (
            self.loops + self.mcp_sessions + self.background_jobs
            + self.unwind + self.gateway_flush + self.stores
        )


@dataclass(frozen=True)
class ShutdownSteps:
    """What the lifespan stops, each step given by the app (see
    ``shut_down`` for the order)."""

    # Periodic loops, event listeners, the boot's connect task.
    loops: Sequence[asyncio.Task[Any]]
    # Leave every MCP endpoint's session manager (``McpEndpoints.close``).
    close_mcp_sessions: Callable[[], Awaitable[None]]
    # Mark every live sandbox, and every one that closes from now on, to
    # be kept for the next boot.
    keep_sandboxes: Callable[[], None]
    # Stop every org runtime, all at once. What their teardown cancels
    # and aborts stays held, for the job drain.
    stop_runtimes: Callable[[], Awaitable[None]]
    flush_gateway_sign_ins: Callable[[], Awaitable[None]]
    # Close the event stream, rate limiter, lock and Mongo.
    close_stores: Callable[[], Awaitable[None]]


class McpEndpoints:
    """The session managers of every mounted MCP endpoint (the gateway,
    the Admin MCP, the operator MCP, the demo), each run in a task of its
    own.

    Starlette's ``Mount`` never runs a mounted app's lifespan, so the app
    lifespan starts them (``start``) and the shutdown leaves them
    (``close``). Leaving a session manager cancels every handler of its
    sessions and waits for them: a gateway tool call writes its audit row
    (up to ``AUDIT_WRITE_TIMEOUT_SECONDS``), an Admin MCP call waits for
    its action (up to ``WAIT_AFTER_CANCEL_SECONDS``). Left one after the
    other, as one ``AsyncExitStack`` did, those waits added up; left at
    once, the slowest one sets the time. A session manager must be left
    by the task that entered it, hence a task each.
    """

    def __init__(self, managers: Sequence[StreamableHTTPSessionManager]) -> None:
        self._managers = list(managers)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False

    async def start(self) -> None:
        """Start every session manager. Once this returns, every endpoint
        answers. Raises what a session manager raised as it started; the
        ones started before it then run until ``close``."""
        loop = asyncio.get_running_loop()
        for index, manager in enumerate(self._managers):
            started: asyncio.Future[None] = loop.create_future()
            task = asyncio.create_task(
                self._serve(manager, started), name=f"mcp-sessions-{index}",
            )
            self._tasks.append(task)
            await asyncio.wait(
                {started, task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if not started.done():
                failure = None if task.cancelled() else task.exception()
                raise failure or RuntimeError(
                    "an MCP session manager ended as it started",
                )

    async def _serve(
        self,
        manager: StreamableHTTPSessionManager,
        started: asyncio.Future[None],
    ) -> None:
        async with manager.run():
            started.set_result(None)
            await self._stop.wait()

    async def close(self) -> None:
        """Leave every session manager at once, and wait until each is
        left. Only the first call does this, the shutdown's, which bounds
        the wait (``ShutdownBudget.mcp_sessions``); a later one returns at
        once, so the lifespan's own exit does not wait for it again."""
        if self._closing:
            return
        self._closing = True
        self._stop.set()
        if not self._tasks:
            return
        await asyncio.wait(self._tasks)
        for task in self._tasks:
            failure = None if task.cancelled() else task.exception()
            if failure is not None:
                logger.error(
                    "app.shutdown.mcp_sessions_failed",
                    task=task.get_name(),
                    exc_info=failure,
                )


async def shut_down(
    drain: DrainCoordinator,
    steps: ShutdownSteps,
    budget: ShutdownBudget | None = None,
) -> None:
    """The lifespan teardown, in an order where nothing outlives what it
    needs.

    1. In-flight requests finish (unless the SIGTERM drain already
       waited for them).
    2. Sandboxes are marked to be kept, first, before anything below cuts
       a connect: one still starting whose only waiter is a loop or a
       tool call cancelled below abandons its start, and closes its
       sandbox session on its own while this waits for the rest (a loop
       whose cleanup awaits a store takes a moment to stop). Its sandbox
       was killed while nothing had marked it yet, which cost the next
       boot a cold start.
    3. Periodic loops and listeners stop, and are waited for.
    4. Every MCP endpoint's sessions close, all at once. Their handlers
       are cancelled; a tool call's audit row still lands (it has its own
       bound), an Admin MCP call waits a while for its action, which goes
       on as a background job if it needs longer. The session managers
       used to be left after Mongo closed: those rows failed
       (``audit.write_failed``) or were lost.
    5. Every org runtime stops, all at once, while the job drain waits for
       every background job: that teardown, an admin action, a sign-in
       warning, an audit write, a sandbox create a runtime aborted (which
       must record the sandbox it keeps). What still runs then is
       cancelled.
    6. The stores close; no background job may start meanwhile.

    Each step waits at most its share of ``budget``. A step still running
    then is logged and, before the job drain, left to it. A step that
    fails is logged and the next ones still run: the stores must close
    whatever happened before.
    """
    budget = budget or ShutdownBudget()
    started = time.monotonic()
    if not drain.is_draining:
        await drain.drain()
    try:
        steps.keep_sandboxes()
    except Exception:
        logger.exception("app.shutdown.step_failed", step="keep_sandboxes")
    for loop_task in steps.loops:
        loop_task.cancel()
    if steps.loops:
        _, still_running = await asyncio.wait(steps.loops, timeout=budget.loops)
        for loop_task in still_running:
            _left_to_the_job_drain.hold(loop_task)
    sessions = await _run_step(
        "mcp_sessions", steps.close_mcp_sessions(), budget.mcp_sessions,
    )
    if sessions is not None:
        _left_to_the_job_drain.hold(sessions)
    # Held, so the job drain below waits for it, and for every connect it
    # aborts from the moment it aborts them.
    _left_to_the_job_drain.spawn(
        _logging_failure("runtimes", steps.stop_runtimes()),
        name="shutdown:runtimes",
    )
    await drain_every_set(budget.background_jobs, unwind_timeout=budget.unwind)
    async with refusing_new_jobs():
        try:
            await asyncio.wait_for(
                steps.flush_gateway_sign_ins(), timeout=budget.gateway_flush,
            )
        except Exception:
            logger.warning("gateway_oauth.shutdown_flush_failed", exc_info=True)
        await _run_step("stores", steps.close_stores(), budget.stores)
    logger.info(
        "app.shutdown.cleanup_done",
        duration_seconds=round(time.monotonic() - started, 3),
    )


async def _run_step(
    step: str, work: Awaitable[None], timeout: float,
) -> asyncio.Task[None] | None:
    """Run one step of ``shut_down``, ``timeout`` seconds at most. Returns
    the step's task if it still runs then, never cancelled here: the
    caller decides what becomes of it."""
    task = asyncio.create_task(
        _logging_failure(step, work), name=f"shutdown:{step}",
    )
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if done:
        return None
    logger.warning(
        "app.shutdown.step_timed_out", step=step, timeout_seconds=timeout,
    )
    return task


async def _logging_failure(step: str, work: Awaitable[None]) -> None:
    """``work``, its failure logged instead of raised: the next steps
    still run."""
    try:
        await work
    except Exception:
        logger.exception("app.shutdown.step_failed", step=step)
