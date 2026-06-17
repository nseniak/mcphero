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

import httpx

# Load-tolerant timeout for the MCP client's HTTP transport. httpx's 5s
# default is too tight when the box is oversubscribed: a tools/list goes
# client -> gateway -> upstream, so every round-trip is two hops.
MCP_CLIENT_TIMEOUT = httpx.Timeout(30.0)


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
