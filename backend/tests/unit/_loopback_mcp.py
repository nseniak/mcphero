"""Shared helpers for unit tests that boot real loopback MCP servers.

Several tests spin up a FastMCP upstream and/or the real MCPolis gateway on
loopback ports and drive them over streamable-HTTP. Two things made that
fragile once the whole suite runs under load — ``run-unit-tests.sh -j auto``
spawns one xdist worker per core, and ``make test-all`` runs a concurrent
e2e + integration suite that competes for the same box:

- **Fixed ports** give no isolation between test files and collide whenever
  an unrelated process happens to hold one. ``free_port`` / ``free_ports``
  hand out OS-assigned ports instead.

- **Readiness waits that only caught ``httpx.ConnectError``** silently let
  ``httpx.ConnectTimeout`` escape. ``ConnectTimeout`` is a *sibling* under
  ``TransportError`` (via ``TimeoutException``), not a subclass of
  ``ConnectError`` — and a starved loopback connect raises exactly that. The
  old loops also gave up after ~5s *without asserting* and let the test
  proceed against a not-yet-serving server, surfacing as a confusing
  downstream failure. ``wait_for_health`` catches the whole transport/OS
  error surface and *raises* on timeout, so a genuine bootstrap failure is
  loud and a transient slow start just retries.
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

T = TypeVar("T")

# Load-tolerant timeout for the MCP client's HTTP transport. httpx's 5s
# default is too tight when the box is oversubscribed: a tools/list goes
# client -> gateway -> upstream, so every round-trip is two hops.
MCP_CLIENT_TIMEOUT = httpx.Timeout(30.0)

# Connection-establishment failures a CPU-starved loopback server raises
# even after ``wait_for_health`` passes — the OS couldn't run the accept
# loop in time, so the connect is refused/timed-out. NOT read/protocol
# errors (those could be real bugs and must surface).
_TRANSIENT_CONNECT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    ConnectionError,
)


def _is_transient_connect_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a transient connect failure.

    The MCP SDK runs the transport inside an anyio TaskGroup, so a
    ``ConnectError`` may surface bare or nested inside an
    ``ExceptionGroup`` / a ``__cause__`` chain. Walk both so either
    shape is recognised; anything else is left to propagate.
    """
    if isinstance(exc, _TRANSIENT_CONNECT_ERRORS):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient_connect_error(e) for e in exc.exceptions)
    nested = exc.__cause__ or exc.__context__
    if nested is not None and nested is not exc:
        return _is_transient_connect_error(nested)
    return False


# Short connect timeout (a blipped loopback accept fails fast and is
# retried) but the full read window for actual MCP round-trips, which can
# legitimately be slow under load. Bounds mcp_session_call's worst case
# well under the 120s per-test ceiling.
_MCP_RETRY_TIMEOUT = httpx.Timeout(30.0, connect=8.0)


async def mcp_session_call(
    url: str,
    token: str,
    fn: Callable[[ClientSession], Awaitable[T]],
    *,
    attempts: int = 4,
    backoff: float = 0.25,
) -> T:
    """Open an authed streamable-HTTP MCP session, run ``fn``, return it.

    Retries the whole connect + ``initialize`` on transient
    connection-establishment failures — the loopback-server analogue of
    the e2e suite's Playwright retries. The server is already
    health-checked up; this only absorbs the OS-level accept blip a
    saturated box produces, never an assertion. A genuinely-broken
    server fails every attempt and the last error propagates, so nothing
    real is masked.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=_MCP_RETRY_TIMEOUT,
            ) as http_client, streamable_http_client(
                url, http_client=http_client,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await fn(session)
        except (Exception, BaseExceptionGroup) as exc:
            if not _is_transient_connect_error(exc):
                raise
            last = exc
            await asyncio.sleep(backoff * (attempt + 1))
    assert last is not None
    raise last


def free_port() -> int:
    """Return one OS-assigned free TCP port on loopback.

    Binds to port 0, reads the assigned port, releases it. There's an
    inherent TOCTOU gap (free *now*, bound by the caller a moment later) but
    for an in-process test server that window is microseconds and a fresh
    OS-assigned port is far less collision-prone than a hard-coded one. Use
    ``free_ports`` when you need several at once.
    """
    return free_ports(1)[0]


def free_ports(n: int) -> list[int]:
    """Return ``n`` distinct OS-assigned free loopback ports.

    Holds every socket open until all ``n`` are bound, so the OS can't hand
    the same port to two back-to-back calls — then releases them together.
    """
    socks: list[socket.socket] = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


# Distinctive, stable phrase raised by ``await_tools_ready`` on timeout:
# a health-OK loopback upstream the starved gateway never managed to
# reach within the window. Under ``make test-all`` the unit rerun net
# retries the whole test on a fresh stack (the unit analogue of the e2e
# leg's retries); a *genuine* "gateway can't reach a healthy upstream"
# bug fails every rerun and is not masked.
UPSTREAM_NEVER_SETTLED = "gateway upstream connection never settled"


async def await_tools_ready(
    url: str, token: str, tool_name: str, *, timeout: float = 40.0,
) -> None:
    """Poll the gateway until ``tool_name`` shows up in ``tools/list``.

    The gateway connects to its upstreams lazily / in a background task,
    so ``tools/list`` is briefly empty even after ``/health`` is OK.
    Under ``make test-all`` load that window stretches (and the
    gateway->upstream *loopback* connect itself can be starved into
    failing), so assertions that read tools right after boot race it
    (the symptom was ``assert 'fake__greet' in []``). Polling makes the
    common case deterministic; each poll also absorbs a transient
    client->gateway connect blip via ``mcp_session_call``. On timeout it
    raises with ``UPSTREAM_NEVER_SETTLED`` so the rerun net retries the
    whole test on a fresh stack — see the note above.

    Polls ``list_tools`` on a SINGLE held session rather than
    reconnecting every tick: under ``-j`` cross-suite load a
    reconnect-per-poll loop is a load firehose (every tick a full
    connect+initialize+teardown), and that self-inflicted load was itself
    widening the very blips it waited on — including the e2e suite's. One
    connect (retried by ``mcp_session_call`` if the first attempt blips),
    then cheap repeated ``list_tools`` on the open session.
    """
    loop = asyncio.get_running_loop()

    async def _poll(session: ClientSession) -> bool:
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            names = [t.name for t in (await session.list_tools()).tools]
            if tool_name in names:
                return True
            await asyncio.sleep(0.5)
        return False

    if not await mcp_session_call(url, token, _poll):
        raise AssertionError(
            f"{UPSTREAM_NEVER_SETTLED}: tool {tool_name!r} absent from "
            f"tools/list after {timeout:.0f}s",
        )


async def wait_for_health(
    *urls: str, timeout: float = 30.0, label: str = "server",
) -> None:
    """Poll each URL until it returns a real HTTP response, or raise.

    A response with status < 500 counts as ready: ``/health`` answers 200,
    a bare ``/mcp`` GET answers 4xx — both prove the app is serving. Catches
    the full transport/OS error surface (refused *and* timed-out connects),
    so a starved loopback connect retries instead of escaping as an uncaught
    ``ConnectTimeout``. Raises ``AssertionError`` if any URL is still not up
    after ``timeout`` seconds — applied per URL.
    """
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
        for url in urls:
            deadline = loop.time() + timeout
            while True:
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        break
                except (httpx.TransportError, OSError):
                    pass
                if loop.time() >= deadline:
                    raise AssertionError(
                        f"{label}: {url} did not become ready within "
                        f"{timeout:.0f}s",
                    )
                await asyncio.sleep(0.1)
