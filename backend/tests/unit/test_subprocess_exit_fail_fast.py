"""Fail-fast detection of subprocess exit during MCP ``initialize``.

Covers the race in ``init_with_exit_race`` (stdio_adapter) plus the
end-to-end behaviour of both sandbox backends when the subprocess
exits before the handshake completes.

Without this, a bogus stdio command (``python3 bogus``) hung the
"Start" action for the full 120s ``INIT_TIMEOUT``. The race surfaces
the failure within ~500ms instead.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, cast

import pytest

from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService
from mcpolis.adapters.sandbox_services.exit_signal import ExitSignalImpl
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    INIT_TIMEOUT,
    StdioInitTimeout,
    SubprocessExitedDuringInit,
    init_with_exit_race,
)
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition


def make_default_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


class _FakeSession:
    """Stub ``ClientSession`` whose ``initialize()`` resolves on demand.

    Lets the race tests exercise the three terminal outcomes
    (init-first, exit-first, timeout) without standing up a real
    ClientSession + transport.
    """

    def __init__(self) -> None:
        self._init_future: asyncio.Future[object] = asyncio.Future()

    async def initialize(self) -> object:
        return await self._init_future

    def resolve_init(self, value: object) -> None:
        if not self._init_future.done():
            self._init_future.set_result(value)

    def fail_init(self, exc: BaseException) -> None:
        if not self._init_future.done():
            self._init_future.set_exception(exc)


def _as_session(s: _FakeSession) -> ClientSession:
    # The race only calls ``initialize()``; the type cast keeps the
    # shared signature without dragging in the full ClientSession ctor.
    return cast(ClientSession, s)


# --- exception formatting -------------------------------------------------


def test_exception_formats_nonzero_exit() -> None:
    exc = SubprocessExitedDuringInit(2, "boom\n")
    assert "code 2" in str(exc)
    # Process output is preserved on the attribute (for logs / "Server
    # logs" panel) but kept out of the user-facing exception message.
    assert "boom" not in str(exc)
    assert exc.exit_code == 2
    assert exc.stderr_tail == "boom\n"


def test_exception_formats_zero_exit() -> None:
    exc = SubprocessExitedDuringInit(0, "")
    assert "code 0" in str(exc)


def test_exception_formats_unknown_exit_code() -> None:
    exc = SubprocessExitedDuringInit(None, "somewhere\n")
    assert "code unknown" in str(exc)
    assert "somewhere" not in str(exc)


# --- race semantics -------------------------------------------------------


@pytest.mark.asyncio
async def test_race_aborts_on_early_exit_nonzero() -> None:
    """Process exits with code 2 while ``initialize`` is still pending
    → fast :class:`SubprocessExitedDuringInit`."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()
    exit_signal.append_stderr(b"boom: not a python script\n")

    async def fire_exit() -> None:
        await asyncio.sleep(0.05)
        exit_signal.mark_exited(2)

    asyncio.create_task(fire_exit())

    started = time.monotonic()
    with pytest.raises(SubprocessExitedDuringInit) as exc_info:
        await init_with_exit_race(_as_session(session), exit_signal)
    elapsed = time.monotonic() - started

    # Generous bound — what matters is that we didn't pay the full
    # ``INIT_TIMEOUT`` (120s).
    assert elapsed < 1.0, (
        f"fail-fast must finish under 1s, took {elapsed:.2f}s"
    )
    assert exc_info.value.exit_code == 2
    assert "boom" in exc_info.value.stderr_tail


@pytest.mark.asyncio
async def test_race_aborts_on_early_exit_zero() -> None:
    """A clean ``sys.exit(0)`` during init still fails fast — an MCP
    server is supposed to stay alive past initialize()."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    async def fire_exit() -> None:
        await asyncio.sleep(0.05)
        exit_signal.mark_exited(0)

    asyncio.create_task(fire_exit())

    with pytest.raises(SubprocessExitedDuringInit) as exc_info:
        await init_with_exit_race(_as_session(session), exit_signal)
    assert exc_info.value.exit_code == 0


@pytest.mark.asyncio
async def test_race_succeeds_when_init_finishes_before_exit() -> None:
    """Init completes first → race returns the init result; subsequent
    exit signals are irrelevant."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    async def fire_init() -> None:
        await asyncio.sleep(0.05)
        session.resolve_init("init-result")

    asyncio.create_task(fire_init())

    result = await init_with_exit_race(_as_session(session), exit_signal)
    assert result == "init-result"


@pytest.mark.asyncio
async def test_race_propagates_init_exception() -> None:
    """If ``initialize()`` raises, the race re-raises that exception
    (NOT :class:`SubprocessExitedDuringInit`)."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    class _Boom(Exception):
        pass

    async def fire_init_failure() -> None:
        await asyncio.sleep(0.05)
        session.fail_init(_Boom("handshake bad"))

    asyncio.create_task(fire_init_failure())

    with pytest.raises(_Boom):
        await init_with_exit_race(_as_session(session), exit_signal)


@pytest.mark.asyncio
async def test_race_times_out_when_neither_signal_fires() -> None:
    """Neither init nor exit fires → :class:`StdioInitTimeout`.

    Uses a small ``timeout`` override so the test doesn't actually
    wait the production 120s.
    """
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    started = time.monotonic()
    with pytest.raises(StdioInitTimeout):
        await init_with_exit_race(
            _as_session(session), exit_signal, timeout=0.2,
        )
    elapsed = time.monotonic() - started
    assert 0.15 < elapsed < 1.0


@pytest.mark.asyncio
async def test_race_timeout_message_points_at_supported_alternatives() -> None:
    """The hang-at-init diagnostic names the supported auth surfaces
    (Variables / Files / streamable-HTTP) so the operator knows where
    to go next instead of just seeing a bare timeout."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    with pytest.raises(StdioInitTimeout) as exc_info:
        await init_with_exit_race(
            _as_session(session), exit_signal, timeout=0.1,
        )

    msg = str(exc_info.value)
    assert "did not initialise" in msg
    assert "browser" in msg
    assert "Variables" in msg
    assert "Files" in msg
    assert "streamable-HTTP" in msg
    assert exc_info.value.timeout_seconds == 0.1


@pytest.mark.asyncio
async def test_race_does_not_leak_tasks_after_exit_path() -> None:
    """Cancelled init task is drained — no "Task was destroyed but it
    is pending" warnings escape."""
    session = _FakeSession()
    exit_signal = ExitSignalImpl()

    async def fire_exit() -> None:
        await asyncio.sleep(0.05)
        exit_signal.mark_exited(1)

    asyncio.create_task(fire_exit())

    with pytest.raises(SubprocessExitedDuringInit):
        await init_with_exit_race(_as_session(session), exit_signal)

    # Settle pending callbacks; if init_task were leaked, it would
    # surface as a warning here.
    await asyncio.sleep(0.05)


# --- ExitSignalImpl unit behaviour ---------------------------------------


def test_exit_signal_first_exit_wins() -> None:
    """``mark_exited`` is idempotent; the first observation is
    authoritative even if the post-reattach E2B watcher fires again."""
    signal = ExitSignalImpl()
    signal.mark_exited(7)
    signal.mark_exited(0)
    snap = signal.snapshot()
    assert snap.exit_code == 7


def test_exit_signal_caps_stderr_tail() -> None:
    """The buffer caps at ``STDERR_TAIL_BYTES`` so a chatty server
    can't blow up per-session memory."""
    from mcpolis.domain.services.sandbox_service import STDERR_TAIL_BYTES

    signal = ExitSignalImpl()
    big = b"x" * (STDERR_TAIL_BYTES + 1024)
    signal.append_stderr(big)
    signal.append_stderr(b"END")
    snap = signal.snapshot()
    assert len(snap.stderr_tail) == STDERR_TAIL_BYTES
    # The tail keeps the most-recent bytes, so the trailing marker
    # must survive.
    assert snap.stderr_tail.endswith("END")


# --- end-to-end through LocalSubprocessSandboxService --------------------


@pytest.mark.asyncio
async def test_local_subprocess_exit_signal_fires_on_bogus_command() -> None:
    """Real ``asyncio.create_subprocess_exec`` against a Python one-liner
    that exits with a known stderr marker; the per-session exit signal
    must fire and capture both the code and the stderr tail."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(
        id="bogus-mcp",
        command=sys.executable,
    )
    # The factory's **kwargs only forwards top-level UpstreamDefinition
    # fields. ``args`` lives on stdio — set it directly so we don't
    # need to extend the factory signature for this single test.
    assert upstream.stdio is not None
    upstream.stdio.args.extend(
        ["-c", "import sys; sys.stderr.write('BOOM\\n'); sys.exit(2)"],
    )

    started = time.monotonic()
    async with service.session(
        session_id="bogus-session",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        await asyncio.wait_for(session.exit_signal.wait(), timeout=2.0)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    snap = session.exit_signal.snapshot()
    assert snap.exit_code == 2
    assert "BOOM" in snap.stderr_tail


@pytest.mark.asyncio
async def test_local_subprocess_init_race_fast_fails_on_bogus_command() -> None:
    """End-to-end: spawn a bogus subprocess via the real
    LocalSubprocessSandboxService and feed the resulting exit signal
    into the race. The race must raise
    :class:`SubprocessExitedDuringInit` within 2s instead of waiting
    out the full ``INIT_TIMEOUT``."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(
        id="bogus-mcp", command=sys.executable,
    )
    assert upstream.stdio is not None
    upstream.stdio.args.extend(
        [
            "-c",
            "import sys; sys.stderr.write('BAD CMD\\n'); sys.exit(2)",
        ],
    )

    fake = _FakeSession()
    started = time.monotonic()
    async with service.session(
        session_id="bogus-session",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as sandbox_session:
        with pytest.raises(SubprocessExitedDuringInit) as exc_info:
            await init_with_exit_race(
                _as_session(fake), sandbox_session.exit_signal,
            )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, (
        f"fail-fast must finish well under INIT_TIMEOUT={INIT_TIMEOUT}s,"
        f" took {elapsed:.2f}s"
    )
    assert exc_info.value.exit_code == 2
    assert "BAD CMD" in exc_info.value.stderr_tail


# Pyright: ``Any`` import preserved for future tests; suppress unused.
_ = Any
