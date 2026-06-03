"""Unit tests for ``SafeAsyncHTTPTransport``.

The transport is the runtime backstop for SSRF: even if a private URL
slipped past create-time validation (DNS rebind, dynamic config
update), every actual outbound request goes through this transport
and gets re-validated.

Three behaviours covered:

- A request to a literal loopback URL fails before any byte hits the
  socket (no test server connection ever recorded).
- A name that resolves public on call #1 and private on call #2 is
  rejected on call #2 (DNS-rebind defence).
- A 302 redirect from a public host to IMDS is refused (cross-host
  redirect re-validation).
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from mcpolis.adapters.upstream_clients.safe_http_transport import (
    SafeAsyncHTTPTransport,
)
from mcpolis.domain.services.url_safety import UnsafeUpstreamUrl


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ConnCounter:
    def __init__(self) -> None:
        self.count = 0


@contextmanager
def _local_server(
    handler: type[BaseHTTPRequestHandler], counter: _ConnCounter,
) -> Iterator[int]:
    port = _free_port()

    class CountingServer(HTTPServer):
        def get_request(self) -> Any:
            counter.count += 1
            return super().get_request()

    server = CountingServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# -------------------------------------------------------- 1: blocked literal


@pytest.mark.asyncio
async def test_loopback_request_fails_before_socket_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport must refuse to dial 127.0.0.1 when the loopback
    test escape hatch is OFF — no bytes hit the local server."""
    monkeypatch.delenv(
        "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", raising=False,
    )

    counter = _ConnCounter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib BaseHTTPRequestHandler API
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # silence test logs
            return

    with _local_server(Handler, counter) as port:
        transport = SafeAsyncHTTPTransport()
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(UnsafeUpstreamUrl):
                await client.get(f"http://127.0.0.1:{port}/")

    assert counter.count == 0


# ----------------------------------------------------- 2: re-resolution / TOCTOU


@pytest.mark.asyncio
async def test_dns_rebind_between_calls_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hostname resolves public on call 1, private on call 2; the
    transport must re-validate and refuse call 2."""
    monkeypatch.delenv(
        "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", raising=False,
    )
    state: dict[str, int] = {"call": 0}

    real = socket.getaddrinfo

    def flipping(
        host: str | None, port: object, *args: object, **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        flags = 0
        if len(args) >= 4:
            flags = int(args[3])  # type: ignore[arg-type]
        elif "flags" in kwargs:
            flags = int(kwargs["flags"])  # type: ignore[arg-type]
        if flags & socket.AI_NUMERICHOST:
            return real(host, port, *args, **kwargs)  # type: ignore[arg-type]
        if host != "rebind.example.com":
            raise socket.gaierror(f"unstubbed host {host!r}")
        state["call"] += 1
        ip = "8.8.8.8" if state["call"] == 1 else "169.254.169.254"
        return [(
            socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0),
        )]

    monkeypatch.setattr(socket, "getaddrinfo", flipping)

    transport = SafeAsyncHTTPTransport()
    # Call 1: validator reports 8.8.8.8 — passes validation. We don't
    # actually want to dial 8.8.8.8 over the network, so we wrap call
    # 1 in a try and only assert that it didn't raise UnsafeUpstreamUrl
    # before the connect-time path.
    async with httpx.AsyncClient(transport=transport) as client:
        try:
            await client.get(
                "http://rebind.example.com/", timeout=0.05,
            )
        except UnsafeUpstreamUrl:
            pytest.fail(
                "validator rejected the first (public) lookup; "
                "DNS-rebind test setup is broken",
            )
        except Exception:
            # ConnectError / timeout connecting to 8.8.8.8 is fine —
            # we only care that validation did not raise.
            pass

        # Call 2: now the resolver hands back IMDS — must be refused.
        with pytest.raises(UnsafeUpstreamUrl):
            await client.get("http://rebind.example.com/")


# ----------------------------------------------------- 3: redirect re-validation


@pytest.mark.asyncio
async def test_redirect_to_imds_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 from a permitted host to ``http://169.254.169.254/`` must
    be re-validated and refused before any bytes go to IMDS."""
    monkeypatch.setenv("MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK", "1")

    counter = _ConnCounter()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header(
                "Location", "http://169.254.169.254/latest/meta-data/",
            )
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    with _local_server(RedirectHandler, counter) as port:
        transport = SafeAsyncHTTPTransport()
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True,
        ) as client:
            with pytest.raises(UnsafeUpstreamUrl):
                await client.get(f"http://127.0.0.1:{port}/")

    # The local server got the initial GET (1 connection); the IMDS
    # redirect target must NEVER have been dialed.
    assert counter.count == 1
