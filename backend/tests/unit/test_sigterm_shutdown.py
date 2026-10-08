"""A stop signal drains the backend, then shuts it down cleanly.

SIGTERM is what ``docker stop`` and every deploy send. The backend must
stop taking new requests, let the in-flight ones finish, then exit
through the lifespan cleanup (sandbox preserve marks, runtime shutdown,
Redis and Mongo close). These tests start the real backend in its own
process, exactly as ``python -m mcpolis`` runs in the container, and
send it SIGTERM.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

_BACKEND = Path(__file__).resolve().parents[2]
_DRAIN_TIMEOUT_SECONDS = 2
_GRACEFUL_SHUTDOWN_SECONDS = 1
# Room for the cleanup itself on a loaded test machine.
_EXIT_MARGIN_SECONDS = 15
_STOP_LIMIT_SECONDS = (
    _DRAIN_TIMEOUT_SECONDS + _GRACEFUL_SHUTDOWN_SECONDS + _EXIT_MARGIN_SECONDS
)
_START_TIMEOUT_SECONDS = 60


def make_app_dir(root: Path) -> Path:
    """A working directory holding the starter config files, the way
    ``docker/docker-entrypoint.sh`` seeds a fresh container."""
    config = root / "config"
    config.mkdir()
    for starter, name in (
        ("config.init.json", "config.json"),
        ("mcp.init.json", "mcp.json"),
        ("oauth_apps.init.json", "oauth_apps.json"),
    ):
        shutil.copy(_BACKEND / "config" / starter, config / name)
    return root


def make_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_backend_env(port: int) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MCPOLIS_")
    }
    env.update(
        PYTHONPATH=f"{_BACKEND / 'src'}{os.pathsep}{_BACKEND}",
        PYTHONDONTWRITEBYTECODE="1",
        MCPOLIS_MODE="standalone",
        MCPOLIS_HOST="127.0.0.1",
        MCPOLIS_PORT=str(port),
        MCPOLIS_DEMO_MOUNT="false",
        MCPOLIS_SENTRY_DSN="",
        MCPOLIS_MIXPANEL_TOKEN="",
        MCPOLIS_DRAIN_TIMEOUT=str(_DRAIN_TIMEOUT_SECONDS),
        MCPOLIS_GRACEFUL_SHUTDOWN_TIMEOUT=str(_GRACEFUL_SHUTDOWN_SECONDS),
        # The dev-stub sign-in, so a test can open a dashboard session.
        MCPOLIS_OAUTH_PROVIDER="dev_stub",
        MCPOLIS_TEST_MODE="1",
    )
    return env


def start_backend(
    app_dir: Path, port: int, log: Path,
) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        return subprocess.Popen(
            [sys.executable, "-m", "mcpolis"],
            cwd=app_dir,
            env=make_backend_env(port),
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def wait_until_listening(proc: subprocess.Popen[bytes], port: int) -> None:
    """Wait until OUR backend answers its health check: a bare open port
    could belong to another process that grabbed it first."""
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the backend exited at start ({proc.returncode})")
        try:
            health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if health.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("the backend never answered its health check")


def make_dashboard_cookie(port: int) -> str:
    """Sign in through the dev-stub flow; return the session cookie."""
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base) as client:
        login = client.get("/api/auth/login")
        state = httpx.URL(login.headers["location"]).params["state"]
        submit = client.get("/api/auth/dev-stub/submit", params={
            "email": "admin@example.com",
            "state": state,
            "redirect_uri": f"{base}/api/auth/callback",
        })
        client.get(submit.headers["location"])
        return client.cookies["mcpolis_session"]


def open_event_stream(port: int, cookie: str) -> socket.socket:
    """Open the dashboard's live event stream, as a browser tab does
    (an EventSource always asks for ``text/event-stream``)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Accept: text/event-stream\r\n"
        f"Cookie: mcpolis_session={cookie}\r\n\r\n".encode(),
    )
    head = sock.recv(4096)
    assert head.startswith(b"HTTP/1.1 200"), head
    return sock


def stopped_within(proc: subprocess.Popen[bytes], seconds: float) -> bool:
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False


def stop_backend_with_sigterm(*, with_open_stream: bool) -> tuple[bool, str]:
    """Start the backend, optionally open a dashboard event stream, send
    SIGTERM. Returns whether it stopped in time, and its log."""
    with tempfile.TemporaryDirectory() as root:
        app_dir = make_app_dir(Path(root))
        log = app_dir / "backend.log"
        port = make_free_port()
        proc = start_backend(app_dir, port, log)
        stream: socket.socket | None = None
        try:
            wait_until_listening(proc, port)
            if with_open_stream:
                stream = open_event_stream(port, make_dashboard_cookie(port))
            proc.send_signal(signal.SIGTERM)
            stopped = stopped_within(proc, _STOP_LIMIT_SECONDS)
        finally:
            if stream is not None:
                stream.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return stopped, log.read_text(errors="replace")


def test_sigterm_drains_then_shuts_the_backend_down() -> None:
    stopped, output = stop_backend_with_sigterm(with_open_stream=False)

    assert "app.sigterm.drain_started" in output, output
    assert stopped, (
        "the backend still ran after SIGTERM: it kept answering 503 and "
        "its shutdown cleanup never ran\n" + output
    )
    assert "Application shutdown complete" in output, output


def test_an_open_dashboard_stream_does_not_hold_the_shutdown() -> None:
    """A dashboard tab keeps its event stream open; a deploy must not
    wait on it until Docker kills the backend, nor let the drain wait for
    it until its timeout."""
    stopped, output = stop_backend_with_sigterm(with_open_stream=True)

    assert stopped, (
        "an open dashboard event stream kept the backend running after "
        "SIGTERM\n" + output
    )
    assert "lifecycle.drain.timeout" not in output, (
        "the drain waited for the open event stream until its timeout\n"
        + output
    )
    assert "Application shutdown complete" in output, output
