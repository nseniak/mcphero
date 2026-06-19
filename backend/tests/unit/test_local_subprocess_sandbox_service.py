"""LocalSubprocessSandboxService — backend-specific tests.

Behaviors that aren't part of the shared SandboxService contract suite:
- ``resume_from`` raises ``NotImplementedError`` (this backend cannot
  resume from a snapshot).
- ``extra_env`` is merged on top of the static stdio config.
- ``map_exit`` distinguishes a non-zero exit from an empty signal.
"""
from __future__ import annotations

import asyncio
import gc
import os
import sys
import tempfile
import tracemalloc
from io import StringIO

import pytest

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    ProviderExitInfo,
    ResourcesUnsupported,
    SandboxResources,
    SnapshotRef,
)
from tests.unit.factories import make_upstream_definition


def make_default_resources() -> SandboxResources:
    """Resources guaranteed to validate against the local backend's
    declared grid. See ``LocalSubprocessSandboxService.capabilities``."""
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


@pytest.mark.asyncio
async def test_session_rejects_resume_from() -> None:
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(id="local-mcp", command="cat")
    snapshot = SnapshotRef(provider="local-subprocess", snapshot_id="x")
    with pytest.raises(NotImplementedError):
        async with service.session(
            session_id="test-session",
            org_id="org",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
            resume_from=snapshot,
        ):
            pass


def test_capabilities_declare_no_isolation() -> None:
    caps = LocalSubprocessSandboxService().capabilities()
    assert caps.supports_pause_resume is False
    assert caps.supports_egress_filtering is False
    assert caps.supports_persistent_disk is False
    # The host subprocess ignores the requested CPU/RAM/disk, so the
    # admin UI hides/disables the resource picker.
    assert caps.enforces_resources is False
    # Disk is not user-configurable on this backend (only the
    # ephemeral 0 GiB choice).
    assert caps.allowed_disk_gb == (0,)


def test_validate_resources_rejects_off_grid_pids_limit_passthrough() -> None:
    """``pids_limit`` is informational on this backend — any value
    (or ``None``) passes validation. Asserted to lock the contract:
    callers shouldn't expect this backend to enforce process caps."""
    service = LocalSubprocessSandboxService()
    service.validate_resources(SandboxResources(
        cpu_vcpus=1.0, memory_mb=1024, disk_gb=0, pids_limit=None,
    ))
    service.validate_resources(SandboxResources(
        cpu_vcpus=1.0, memory_mb=1024, disk_gb=0, pids_limit=99_999,
    ))


def test_validate_resources_error_carries_field_name() -> None:
    service = LocalSubprocessSandboxService()
    with pytest.raises(ResourcesUnsupported) as exc:
        service.validate_resources(SandboxResources(
            cpu_vcpus=1024.0, memory_mb=1024, disk_gb=0,
        ))
    assert exc.value.field == "cpu_vcpus"
    assert exc.value.value == 1024.0


def test_map_exit_subprocess_exited() -> None:
    service = LocalSubprocessSandboxService()
    reason, detail = service.map_exit(
        ProviderExitInfo(exit_code=137, raw_message="killed"),
    )
    assert reason is ExitReason.SUBPROCESS_EXITED
    assert detail == "killed"


def test_map_exit_zero_exit_is_internal_error() -> None:
    """Local subprocesses that exit cleanly without producing any
    EXIT signal end up here; ``INTERNAL_ERROR`` (rather than
    ``SUBPROCESS_EXITED``) so admins know the backend lost track of
    the process state."""
    service = LocalSubprocessSandboxService()
    reason, detail = service.map_exit(ProviderExitInfo(exit_code=0))
    assert reason is ExitReason.INTERNAL_ERROR
    assert detail is None


def test_map_exit_no_signal_at_all() -> None:
    service = LocalSubprocessSandboxService()
    reason, detail = service.map_exit(ProviderExitInfo())
    assert reason is ExitReason.INTERNAL_ERROR
    assert detail is None


@pytest.mark.asyncio
async def test_session_starts_subprocess_and_logs_lifecycle() -> None:
    """Smoke: session() actually spawns the configured command. We
    use ``cat`` and let the context exit immediately; the process
    is reaped during cleanup."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(id="local-mcp", command="cat")
    async with service.session(
        session_id="test-session",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        read_stream, write_stream = session.read_stream, session.write_stream
        assert read_stream is not None
        assert write_stream is not None


@pytest.mark.asyncio
async def test_session_missing_command_names_the_executable() -> None:
    """A missing command must surface its name, not a bare
    "[Errno 2] No such file or directory" — ``str(exc)`` is what reaches
    the dashboard's "Couldn't connect" line (M3)."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(
        id="missing-mcp", command="npx-not-installed-xyz",
    )
    with pytest.raises(FileNotFoundError) as exc:
        async with service.session(
            session_id="test-session",
            org_id="org",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            pass
    message = str(exc.value)
    assert "npx-not-installed-xyz" in message
    assert "not found" in message
    # The raw OSError prefix must not leak through.
    assert "Errno 2" not in message


@pytest.mark.asyncio
async def test_session_routes_stderr_to_errlog() -> None:
    """``errlog`` receives the subprocess's stderr stream verbatim.

    Spawn a tiny shell command that writes a known marker to stderr
    and exits; the marker must appear in the supplied buffer.
    """
    service = LocalSubprocessSandboxService()
    # ``sh -c`` so we can write a known marker to stderr without
    # depending on a Python interpreter being on PATH; sleep 0.5 so
    # the read pump has time to drain the pipe before EOF.
    upstream = make_upstream_definition(
        id="stderr-mcp",
        command="sh",
    )
    upstream.stdio.args = ["-c", "echo MARKER-XYZ 1>&2; sleep 0.5"]  # type: ignore[union-attr]
    sink = StringIO()
    async with service.session(
        session_id="test-session",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
    ):
        # Give the subprocess and stdio_client's pump enough time to
        # write through before we tear the context down. 1s is
        # generous on dev hardware; flake-resistant.
        await asyncio.sleep(1.0)
    assert "MARKER-XYZ" in sink.getvalue()


@pytest.mark.asyncio
async def test_session_extra_env_overrides_upstream_env() -> None:
    """``extra_env`` is merged on top of ``upstream.stdio.env``;
    keys collision wins for the per-session value."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(
        id="env-mcp",
        command="sh",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-c", 'echo "VAL=${MCP_AUTH_TOKEN:-unset}" 1>&2; sleep 0.5',
    ]
    upstream.stdio.env = {"MCP_AUTH_TOKEN": "static-token"}  # type: ignore[union-attr]
    sink = StringIO()
    async with service.session(
        session_id="test-session",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
        extra_env={"MCP_AUTH_TOKEN": "session-token"},
    ):
        await asyncio.sleep(1.0)
    captured = sink.getvalue()
    assert "VAL=session-token" in captured
    assert "static-token" not in captured


def test_sandbox_home_is_isolated_per_session() -> None:
    """The home is a per-session temp dir under ``$TMPDIR`` —
    deterministic per ``session_id``, distinct across sessions, and
    never the operator's real ``$HOME``. This is what makes ``${HOME}``
    resolvable on a backend that spawns plain host subprocesses."""
    service = LocalSubprocessSandboxService()
    a = service.sandbox_home(session_id="sid-a")
    b = service.sandbox_home(session_id="sid-b")
    assert a.startswith(tempfile.gettempdir())
    assert "sid-a" in a
    assert a == service.sandbox_home(session_id="sid-a")  # deterministic
    assert a != b
    assert a != os.environ.get("HOME")


@pytest.mark.asyncio
async def test_session_home_round_trips_templated_file() -> None:
    """Regression for the provider-aware ``${HOME}`` bug.

    A ``${HOME}``-templated sandbox file must materialize where the
    spawned process's ``$HOME`` points. The session forces the
    subprocess's ``HOME`` to its per-session temp dir, and ``${HOME}``
    already substituted (in the manager) to that same dir — so the file
    round-trips. Before the fix, ``${HOME}`` resolved to E2B's
    ``/home/user`` while the subprocess inherited the host home; the two
    never agreed and the file never materialized where the MCP looked.

    The subprocess reads the file via its OWN ``$HOME`` (``cat
    "$HOME/.config/cred.txt"``), proving the equality end-to-end. After
    teardown the per-session home is removed.
    """
    service = LocalSubprocessSandboxService()
    session_id = "round-trip-1"
    home = service.sandbox_home(session_id=session_id)
    expected = "secret-cred-body"
    target_path = os.path.join(home, ".config", "cred.txt")
    upstream = make_upstream_definition(id="home-mcp", command="sh")
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-c",
        'echo "HOME=$HOME" 1>&2; cat "$HOME/.config/cred.txt" 1>&2; sleep 0.5',
    ]
    sink = StringIO()
    async with service.session(
        session_id=session_id,
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
        materialize_files=[
            MaterializeFile(
                name="cred", contents=expected, target_path=target_path,
            ),
        ],
    ):
        await asyncio.sleep(1.0)
    captured = sink.getvalue()
    # The subprocess's $HOME is exactly the session's sandbox home...
    assert f"HOME={home}" in captured
    # ...and reading $HOME-relative finds the materialized contents.
    assert expected in captured
    # Per-session home (and the file under it) cleaned up on teardown.
    assert not os.path.exists(home)


async def test_pause_returns_none() -> None:
    service = LocalSubprocessSandboxService()
    assert await service.pause(session_id="any") is None


# ---------- SBX-7 [BUG?]: oversized non-JSON stdout (local) ----------


# Emits a big blob that NEVER contains a newline, then idles so the test
# can observe how much of it the pump retains in its leftover ``buffer``.
_NO_NEWLINE_STDOUT_SCRIPT = (
    "import sys, time\n"
    'sys.stdout.write("x" * (16 * 1024 * 1024))\n'  # 16 MiB, no newline
    "sys.stdout.flush()\n"
    "time.sleep(3.0)\n"
)


@pytest.mark.asyncio
async def test_sbx7_local_no_newline_stream_buffer_is_bounded() -> None:
    """[BUG?] SBX-7 (P1): a local-subprocess stdout stream that never
    emits a newline must not be retained in memory unbounded. The pump
    appends every read to ``buffer`` and ``split('\\n')`` leaves it all as
    the leftover, so 16 MiB of newline-free output is held verbatim.

    Pinned via tracemalloc RETENTION (not errlog size): with no newline,
    nothing is ever flushed to errlog, so an errlog-only cap would not
    touch this DoS. We poll until the pump has clearly accumulated the
    blob (bug present) or a generous timeout elapses (a bounded fix)."""
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(id="big-mcp", command=sys.executable)
    upstream.stdio.args = ["-c", _NO_NEWLINE_STDOUT_SCRIPT]  # type: ignore[union-attr]
    sink = StringIO()
    sane_retained = 4 * 1024 * 1024  # 4 MiB: 4x below the 16 MiB fed
    async with service.session(
        session_id="sbx7-local-nonl",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
    ) as session:
        async def drain() -> None:
            try:
                async with session.read_stream:
                    async for item in session.read_stream:
                        del item
            except Exception:
                pass

        drain_task = asyncio.create_task(drain())
        gc.collect()
        tracemalloc.start()
        base, _ = tracemalloc.get_traced_memory()
        retained = 0
        # Break early once retention clearly exceeds the bound (bug
        # present → fast, deterministic RED); otherwise poll the full
        # window (a bounded fix → stays low → green).
        for _ in range(40):  # up to ~2s
            await asyncio.sleep(0.05)
            current, _ = tracemalloc.get_traced_memory()
            retained = current - base
            if retained > sane_retained * 2:
                break
        tracemalloc.stop()
        drain_task.cancel()
    assert retained <= sane_retained, (
        f"no-newline stdout retained {retained} bytes in memory; "
        f"the leftover buffer is unbounded"
    )


@pytest.mark.asyncio
async def test_sbx7_local_chatty_lines_all_route_to_errlog() -> None:
    """SBX-7 (P1) — chatty path (well-behaved): many newline-framed
    non-JSON lines each route to errlog. This branch is bounded per
    line, so it passes; documents the no-bug half of the spec."""
    n = 2000
    script = (
        "import sys, time\n"
        f"for i in range({n}):\n"
        '    sys.stdout.write(f"npm warn deprecated pkg-{i}@1.0.0\\n")\n'
        "sys.stdout.flush()\n"
        "time.sleep(2.0)\n"
    )
    service = LocalSubprocessSandboxService()
    upstream = make_upstream_definition(id="chatty-mcp", command=sys.executable)
    upstream.stdio.args = ["-c", script]  # type: ignore[union-attr]
    sink = StringIO()
    async with service.session(
        session_id="sbx7-local-chatty",
        org_id="org",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
    ) as session:
        async def drain() -> None:
            try:
                async with session.read_stream:
                    async for item in session.read_stream:
                        del item
            except Exception:
                pass

        drain_task = asyncio.create_task(drain())
        await asyncio.sleep(1.5)
        drain_task.cancel()
    assert sink.getvalue().count("npm warn deprecated") == n, (
        f"all {n} chatty lines must route to errlog; "
        f"got {sink.getvalue().count('npm warn deprecated')}"
    )


# ---------- SBX-10 [BUG?]: materialize write failure leaks temp home -----


@pytest.mark.asyncio
async def test_sbx10_local_materialize_write_failure_cleans_up_home() -> None:
    """[BUG?] SBX-10 (P1): a materialize write to an unwritable path
    must surface a clean error AND not leak the per-session temp home.
    We force the write to fail by making a parent path component a FILE
    (so ``mkdir(parents=True)`` raises ``NotADirectoryError``). Intended:
    the temp home is cleaned up on the failure. Observed: it is left
    behind (only the spawn-except / teardown clean it, and neither runs
    when the write raises first)."""
    service = LocalSubprocessSandboxService()
    session_id = "sbx10-leak"
    home = service.sandbox_home(session_id=session_id)
    os.makedirs(home, exist_ok=True)
    # Plant a regular file where a directory component is expected, so
    # the materialize ``mkdir(parents=True)`` raises NotADirectoryError.
    file_as_dir = os.path.join(home, "afile")
    with open(file_as_dir, "w", encoding="utf-8") as fh:
        fh.write("x")
    target_path = os.path.join(file_as_dir, "sub", "cred.txt")

    upstream = make_upstream_definition(id="local-mcp", command="cat")
    try:
        with pytest.raises(OSError):
            async with service.session(
                session_id=session_id,
                org_id="org",
                upstream=upstream,
                resources=make_default_resources(),
                denylist=(),
                materialize_files=[
                    MaterializeFile(
                        name="cred", contents="body",
                        target_path=target_path,
                    ),
                ],
            ):
                pass
        # The temp home must NOT be leaked after a materialize failure.
        assert not os.path.exists(home), (
            f"materialize failure leaked the per-session temp home: {home!r}"
        )
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)
