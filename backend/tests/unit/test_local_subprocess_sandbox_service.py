"""LocalSubprocessSandboxService — backend-specific tests.

Behaviors that aren't part of the shared SandboxService contract suite:
- ``resume_from`` raises ``NotImplementedError`` (this backend cannot
  resume from a snapshot).
- ``extra_env`` is merged on top of the static stdio config.
- ``map_exit`` distinguishes a non-zero exit from an empty signal.
"""
from __future__ import annotations

import asyncio
from io import StringIO

import pytest

from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.adapters.sandbox_services import LocalSubprocessSandboxService
from mcpolis.domain.services.sandbox_service import (
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


async def test_pause_returns_none() -> None:
    service = LocalSubprocessSandboxService()
    assert await service.pause(session_id="any") is None
