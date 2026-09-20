"""E2BSandboxService — backend-specific tests.

Exercises the parts of the SandboxService surface that don't fit the
generic contract suite: template name lookup, mcpolis_instance
metadata tagging, on_timeout backstop, mock-SDK-driven session
lifecycle, and ``map_exit`` mapping for E2B-specific error classes.

A real-SDK smoke gate lives in
``test_e2b_sandbox_service_real_sdk.py`` (CI-only); this file is
fully mocked and runs offline.
"""
from __future__ import annotations

import asyncio
import gc
import time
import tracemalloc
from datetime import UTC, datetime
from io import StringIO
from typing import Any, cast

import anyio
import pytest

from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.adapters.sandbox_e2b import (
    E2BAuthError,
    E2BNotFoundError,
    E2BQuotaError,
    E2BSandboxService,
    E2BSDKError,
    E2BTemplateGrid,
    template_name_for,
)
from mcpolis.adapters.sandbox_e2b.service import (
    PERSISTENT_VOLUME_MOUNT_PATH,
    VOLUME_METADATA_KEY,
)
from mcpolis.adapters.sandbox_e2b.template_grid import language_for_command
from mcpolis.domain.services.exit_reason import ExitReason
from mcpolis.domain.model.upstream import (
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_service import (
    ProviderExitInfo,
    ResourcesUnsupported,
    SandboxResources,
    SnapshotRef,
)
from tests.unit.factories import make_upstream_auth, make_upstream_definition
from tests.unit.sandbox_e2b_mock import (
    MockE2BClient,
    MockE2BProcessHandle,
    MockE2BSandboxHandle,
    make_mock_e2b_client,
)


def make_default_resources(
    cpu_vcpus: float = 1.0, memory_mb: int = 1024,
) -> SandboxResources:
    return SandboxResources(
        cpu_vcpus=cpu_vcpus, memory_mb=memory_mb, disk_gb=0,
    )


def make_e2b_service(
    *,
    client: MockE2BClient | None = None,
    mcpolis_instance: str = "test-instance",
    persistence: SandboxPersistenceRepository | None = None,
    volumes_enabled: bool = True,
) -> tuple[E2BSandboxService, MockE2BClient]:
    """Builder for the E2B service with a fresh mock client.

    Defaults ``volumes_enabled=True`` so existing tests that pass
    a persistence repo see the volume code path; tests that
    specifically exercise the disabled-flag behaviour pass
    ``volumes_enabled=False``.
    """
    real_client = client if client is not None else make_mock_e2b_client()
    return (
        E2BSandboxService(
            real_client,
            mcpolis_instance=mcpolis_instance,
            on_timeout_seconds=60,
            persistence=persistence,
            volumes_enabled=volumes_enabled,
        ),
        real_client,
    )


def make_persistent_disk_upstream(
    *, id: str = "ups-vol", command: str = "npx",
) -> UpstreamDefinition:
    """Builder for an upstream with persistent_disk_enabled=True.

    The factory in ``tests/factories.py`` doesn't expose a way to flip
    this stdio-config flag, so tests construct the model directly.
    """
    return UpstreamDefinition(
        id=id,
        display_name=id,
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command=command, persistent_disk_enabled=True,
        ),
        auth=make_upstream_auth(),
    )


# ---------- template grid ----------


def test_template_grid_resolves_known_combo() -> None:
    name = template_name_for(language="node", cpu_vcpus=2, memory_mb=4096)
    assert name == "mcpolis-node-cpu2-ram4096"


def test_template_grid_rejects_off_grid() -> None:
    with pytest.raises(KeyError):
        template_name_for(language="node", cpu_vcpus=1, memory_mb=8192)


def test_template_grid_rejects_non_integer_cpu() -> None:
    """The published grid only carries integer-CPU templates; a 0.5
    vCPU request must fail rather than silently resolving to the
    closest integer."""
    grid = E2BTemplateGrid()
    assert grid.is_valid_pairing(cpu_vcpus=0.5, memory_mb=1024) is False


def test_language_for_command_maps_npx_and_uvx() -> None:
    assert language_for_command("npx") == "node"
    assert language_for_command("NPX") == "node"
    assert language_for_command("uvx") == "python"
    assert language_for_command("python3") == "python"


def test_language_for_command_maps_docker() -> None:
    assert language_for_command("docker") == "docker"
    assert language_for_command("DOCKER") == "docker"
    assert language_for_command("  docker  ") == "docker"


def test_language_for_command_returns_none_for_unknown() -> None:
    assert language_for_command("perl") is None
    assert language_for_command("cat") is None


# ---------- capabilities + validation ----------


def test_capabilities_hide_disk_grid() -> None:
    """Storage isn't user-configurable on E2B (template-time fixed),
    so the disk grid surfaces empty so the admin UI can hide the
    control entirely."""
    service, _ = make_e2b_service()
    caps = service.capabilities()
    assert caps.allowed_disk_gb == ()
    assert caps.supports_persistent_disk is False
    assert caps.supports_egress_filtering is False
    # E2B enforces the picked CPU/RAM (template-backed), so the admin
    # UI keeps the resource picker live.
    assert caps.enforces_resources is True


def test_sandbox_home_is_fixed_home_user() -> None:
    """E2B containers run as the stock SDK user (``user``) with
    ``HOME=/home/user``, fixed regardless of session — so ``${HOME}``
    resolves there. Guarded against template drift by
    ``test_e2b_template_home_consistency.py``."""
    service, _ = make_e2b_service()
    assert service.sandbox_home(session_id="anything") == "/home/user"
    assert (
        service.sandbox_home(session_id="other")
        == service.sandbox_home(session_id="anything")
    )


def test_validate_resources_rejects_non_zero_disk() -> None:
    service, _ = make_e2b_service()
    with pytest.raises(ResourcesUnsupported) as exc:
        service.validate_resources(SandboxResources(
            cpu_vcpus=1.0, memory_mb=1024, disk_gb=5,
        ))
    assert exc.value.field == "disk_gb"


def test_validate_resources_rejects_off_grid_pairing() -> None:
    """1 vCPU + 8 GiB isn't in the published grid (we don't burn
    template-build time on imbalanced combos). Reject explicitly."""
    service, _ = make_e2b_service()
    with pytest.raises(ResourcesUnsupported):
        service.validate_resources(SandboxResources(
            cpu_vcpus=1.0, memory_mb=8192, disk_gb=0,
        ))


def test_validate_resources_accepts_every_published_pairing() -> None:
    """Every (cpu, ram) entry in the published grid must validate."""
    service, _ = make_e2b_service()
    grid = E2BTemplateGrid()
    for cpu, ram in grid.cpu_ram_pairs:
        service.validate_resources(SandboxResources(
            cpu_vcpus=float(cpu), memory_mb=ram, disk_gb=0,
        ))


# ---------- session: SDK calls ----------


@pytest.mark.asyncio
async def test_session_creates_sandbox_with_correct_template() -> None:
    """The service derives the template from the upstream's command
    + the SandboxResources passed in, and calls Sandbox.create with
    exactly that template name."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    upstream.stdio.args = ["-y", "@modelcontextprotocol/server-everything"]  # type: ignore[union-attr]
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(cpu_vcpus=2.0, memory_mb=4096),
        denylist=(),
    ):
        pass
    assert len(mock.creates) == 1
    assert mock.creates[0].template == "mcpolis-node-cpu2-ram4096"


@pytest.mark.asyncio
async def test_session_tags_sandbox_with_mcpolis_metadata() -> None:
    """Every Sandbox.create call carries the mcpolis attribution
    tags. Reconcilers (step 10) need this to distinguish "my
    sandboxes" from a different mcpolis instance's."""
    service, mock = make_e2b_service(mcpolis_instance="instance-blue-7")
    upstream = make_upstream_definition(id="ups-x", command="uvx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert mock.creates[0].metadata == {
        "mcpolis_org": "acme",
        "mcpolis_upstream": "ups-x",
        "mcpolis_instance": "instance-blue-7",
    }


@pytest.mark.asyncio
async def test_session_threads_on_timeout_seconds_into_sandbox_create() -> None:
    """The configured on_timeout=pause backstop reaches every
    Sandbox.create, so a backend crash can't leak compute beyond
    that window of orphan time."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert mock.creates[0].timeout_seconds == 60


@pytest.mark.asyncio
async def test_session_runs_command_with_merged_env() -> None:
    """``extra_env`` (auth token from SandboxConnectionTask) is
    merged on top of the static stdio config env, with extras
    winning on collision."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    upstream.stdio.env = {"NODE_ENV": "test", "MCP_AUTH_TOKEN": "static"}  # type: ignore[union-attr]
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        extra_env={"MCP_AUTH_TOKEN": "session"},
    ):
        pass
    assert len(mock.commands) == 1
    cmd = mock.commands[0]
    assert cmd.argv[0] == "npx"
    assert cmd.env["NODE_ENV"] == "test"
    assert cmd.env["MCP_AUTH_TOKEN"] == "session"


@pytest.mark.asyncio
async def test_session_injects_npm_uv_log_defaults() -> None:
    """Every E2B session injects NPM/UV verbosity defaults so cold
    install output reaches the operator's "Server logs" pane.
    Mirrors the ``--env=`` flags the own-runner sets in
    ``runner/internal/runtime/podman/podman.go``."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    cmd_env = mock.commands[0].env
    assert cmd_env["NPM_CONFIG_LOGLEVEL"] == "info"
    assert cmd_env["NPM_CONFIG_FUND"] == "false"
    assert cmd_env["NPM_CONFIG_AUDIT"] == "false"
    assert cmd_env["NPM_CONFIG_PROGRESS"] == "false"
    assert cmd_env["UV_NO_PROGRESS"] == "1"


@pytest.mark.asyncio
async def test_session_npm_defaults_overridden_by_upstream_env() -> None:
    """If an upstream explicitly sets ``NPM_CONFIG_LOGLEVEL=warn``,
    its choice wins — defaults are only safety-nets for the silent
    case."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    upstream.stdio.env = {"NPM_CONFIG_LOGLEVEL": "warn"}  # type: ignore[union-attr]
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert mock.commands[0].env["NPM_CONFIG_LOGLEVEL"] == "warn"


@pytest.mark.asyncio
async def test_session_writes_unparsable_stdout_to_errlog() -> None:
    """Non-JSON-RPC stdout chatter (npm install lines that print to
    stdout under some configurations) goes to ``errlog`` so the
    operator's "Server logs" pane isn't silent during cold installs.
    Without this fan-out, parse-failed lines would be silently
    consumed as Exceptions on the read stream."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    errlog = StringIO()
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=errlog,
    ) as session:
        read_stream = session.read_stream
        # Drain the read stream concurrently — the parse-failed
        # branch sends the Exception to read_writer with buffer
        # size 0, which would block forever without a consumer.
        async def drain() -> Exception | None:
            async with read_stream:
                async for item in read_stream:
                    if isinstance(item, Exception):
                        return item
            return None

        drain_task = asyncio.create_task(drain())
        # Simulate the SDK delivering a chatty stdout line that isn't
        # JSON-RPC. The on_stdout callback is what the real SDK calls
        # under the hood — we drive it directly via the captured ref.
        on_stdout = mock.last_on_stdout
        assert on_stdout is not None
        result = on_stdout(b"npm warn deprecated foo@1.2.3\n")
        if asyncio.iscoroutine(result):
            await result
        # Let the drain pick up the Exception, then cancel.
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()
    captured = errlog.getvalue()
    assert "npm warn deprecated foo@1.2.3" in captured


@pytest.mark.asyncio
async def test_session_rejects_unknown_command_language() -> None:
    """Commands that don't map to a published-grid language fail
    fast with ResourcesUnsupported rather than picking a surprising
    fallback."""
    service, _ = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="perl")
    with pytest.raises(ResourcesUnsupported):
        async with service.session(
            session_id="contract-session",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            pass


@pytest.mark.asyncio
async def test_session_kills_sandbox_on_clean_exit() -> None:
    """The default close path kills the sandbox so a graceful
    teardown doesn't leak compute."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert len(mock.kills) == 1
    assert mock.kills[0].sandbox_id.startswith("sbx-")


@pytest.mark.asyncio
async def test_session_resume_from_snapshot_calls_connect() -> None:
    """When ``resume_from`` is set, the service calls
    Sandbox.connect(snapshot_id) instead of Sandbox.create."""
    service, mock = make_e2b_service()
    # Create a sandbox + pause it via the mock's machinery so we have
    # a known-good snapshot id to resume from.
    seed_handle = await mock.create_sandbox(
        template="mcpolis-node-cpu1-ram1024",
        metadata={"mcpolis_org": "acme"},
        timeout_seconds=3600,
    )
    snapshot_id = await seed_handle.pause()
    mock.creates.clear()
    mock.connects.clear()

    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        resume_from=SnapshotRef(provider="e2b", snapshot_id=snapshot_id),
    ):
        pass
    assert len(mock.creates) == 0
    assert len(mock.connects) == 1
    assert mock.connects[0].snapshot_id == snapshot_id


@pytest.mark.asyncio
async def test_session_rejects_resume_from_other_provider() -> None:
    service, _ = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    with pytest.raises(ValueError):
        async with service.session(
            session_id="contract-session",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
            resume_from=SnapshotRef(
                provider="own-runner", snapshot_id="oops",
            ),
        ):
            pass


# ---------- session: stdio bridging ----------


@pytest.mark.asyncio
async def test_session_streams_stderr_to_errlog() -> None:
    """on_stderr callbacks installed via the SDK funnel into the
    caller's TextIO, same as the local-subprocess pipe drain."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    sink = StringIO()
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        errlog=sink,
    ):
        # Simulate provider-side stderr emission via the captured cb.
        cb = mock.last_on_stderr
        assert cb is not None
        result = cb(b"MARKER-XYZ\n")
        if asyncio.iscoroutine(result):
            await result
    assert "MARKER-XYZ" in sink.getvalue()


from collections.abc import Awaitable, Callable


async def _invoke_cb_concurrently(
    cb: Callable[[bytes], Awaitable[None] | None], payload: bytes,
) -> asyncio.Task[None]:
    """The service's on_stdout/on_stderr callbacks send onto a
    0-buffer anyio memory stream — they only complete when the
    receiver consumes the message. Run the call as a task so the
    test body can ``receive()`` it without deadlocking."""

    async def _run() -> None:
        result: Any = cb(payload)
        if asyncio.iscoroutine(result):
            await result

    return asyncio.create_task(_run())


@pytest.mark.asyncio
async def test_session_demuxes_stdout_into_jsonrpc_messages() -> None:
    """Newline-framed JSON-RPC bytes coming back via on_stdout get
    re-assembled into SessionMessages on the read stream."""
    from mcp import types as mcp_types
    from mcp.shared.message import SessionMessage

    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        read_stream = session.read_stream
        cb = mock.last_on_stdout
        assert cb is not None
        msg = mcp_types.JSONRPCMessage.model_validate({
            "jsonrpc": "2.0", "method": "notifications/initialized",
            "params": {},
        })
        line = msg.model_dump_json(by_alias=True, exclude_none=True)
        feeder = await _invoke_cb_concurrently(cb, (line + "\n").encode())
        received = await asyncio.wait_for(read_stream.receive(), timeout=2.0)
        await asyncio.wait_for(feeder, timeout=2.0)
        assert isinstance(received, SessionMessage)


@pytest.mark.asyncio
async def test_session_surfaces_stdin_send_failures_on_read_stream() -> None:
    """If the underlying sandbox has died (E2B kill timer fired, network
    blip), ``send_stdin`` raises an SDK error. The session must fail the
    transport so the wrapping ClientSession fails any in-flight tool
    call instead of hanging forever waiting for a stdout response that
    will never come.

    Regression for the 2026-04-30 incident: three MCPs went silent
    after several hours; the sandboxes had been killed by E2B's
    on_timeout=kill default and stdin_pump silently swallowed the
    SDK error, leaving ClientSession blocked on read_stream.

    Fail-fast contract (updated 2026-05-25): the pump CLOSES the read
    side and sets ``transport_failed`` rather than sending an Exception
    object down it. MCP SDK >=1.x routes a streamed Exception to the
    message handler (which drops it), so the old approach left the
    request hanging out its full timeout; closing the stream ends the
    ClientSession read loop, which fails pending requests at once.
    """
    from mcp import types as mcp_types
    from mcp.shared.message import SessionMessage

    service, _ = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="dead-sandbox-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        read_stream, write_stream = session.read_stream, session.write_stream
        live_handle = service._live_sandboxes["dead-sandbox-session"]  # type: ignore[reportPrivateUsage]
        process = live_handle.last_process  # type: ignore[reportAttributeAccessIssue]
        assert process is not None
        process.stdin_send_error = E2BSDKError(
            "E2BNotFoundError", "sandbox sbx-0 not found",
        )

        request = SessionMessage(
            message=mcp_types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }),
        )
        await write_stream.send(request)

        # Read side closed -> EndOfStream (the ClientSession read loop
        # would end here and fail every pending request).
        with pytest.raises(anyio.EndOfStream):
            await asyncio.wait_for(read_stream.receive(), timeout=2.0)
        assert session.transport_failed is not None
        assert session.transport_failed.is_set()


@pytest.mark.asyncio
async def test_session_retires_process_after_stream_dies() -> None:
    """A woken sandbox must retire its MCP process, not reattach to it.

    E2B's ``on_timeout=pause`` severs the streaming RPC behind the
    original ``run_command``. The session used to reattach to the same
    pid here, which handed back a process frozen mid-flight: its HTTP
    client still believed it owned pooled TCP connections that were
    severed while it slept, so the first calls after a wake failed with
    ECONNRESET, one per pooled socket (20 of 20 wakes reproduced in
    ``tests/integration/diagnose_wake_network.py``).

    The pump now kills the frozen process and fails the transport, so
    the manager rebuilds the session against the same sandbox with a
    fresh process. Critically, the pending frame must NOT be written to
    anything: it has not reached the upstream, so the rebuilt session
    can deliver it exactly once with no risk of double execution.
    """
    from typing import cast

    from mcp import types as mcp_types
    from mcp.shared.message import SessionMessage

    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="retire-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        write_stream = session.write_stream
        live_handle = cast(
            MockE2BSandboxHandle,
            service._live_sandboxes["retire-session"],  # type: ignore[reportPrivateUsage]
        )
        original_process = live_handle.last_process
        assert original_process is not None
        original_pid = original_process.pid

        # Simulate the streaming RPC dying — wait() returns, watcher
        # sets ``stream_dead``. Yield to the loop so the watcher runs
        # before the pump's next iteration peeks at the flag.
        original_process.simulate_exit(0)
        for _ in range(5):
            await asyncio.sleep(0)

        request = SessionMessage(
            message=mcp_types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }),
        )
        await write_stream.send(request)

        for _ in range(50):
            if mock.kill_commands:
                break
            await asyncio.sleep(0.01)

        assert len(mock.kill_commands) == 1, (
            f"the frozen process must be killed; kills={mock.kill_commands}"
        )
        assert mock.kill_commands[0].pid == original_pid
        assert not mock.connect_commands, (
            "a woken sandbox must never reattach to its frozen process; "
            f"connect_commands={mock.connect_commands}"
        )
        assert session.transport_failed is not None
        for _ in range(50):
            if session.transport_failed.is_set():
                break
            await asyncio.sleep(0.01)
        assert session.transport_failed.is_set(), (
            "retiring the process must fail the transport so the manager "
            "rebuilds the session"
        )
        # Exactly-once: the frame never reached a process, so the
        # rebuilt session owns its only delivery.
        assert not original_process.stdin_buffer, (
            "the pending frame must not be written to the frozen process"
        )


@pytest.mark.asyncio
async def test_stream_death_marks_the_transport_dead_immediately() -> None:
    """The wake design rests entirely on this one ordering.

    E2B severs the output stream when it pauses a sandbox, so the
    watcher learns of the pause AT the pause — measured 8.7s before the
    next request arrived, in a real integration run. Declaring the
    transport dead right there is what lets
    ``ensure_shared_connected`` refuse the session and rebuild BEFORE
    the gateway writes anything. No request is handed to a frozen
    process, so none is lost, so none has to be re-sent.

    Nothing writes to the session in this test. If ``transport_failed``
    only became set once the pump tripped over a frame, the request
    that tripped it would already have been lost, and we would be back
    to needing a "may I re-send this?" proof — which two rounds of
    adversarial review each punctured.
    """
    from typing import cast

    service, _mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="stream-death",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        live_handle = cast(
            MockE2BSandboxHandle,
            service._live_sandboxes["stream-death"],  # type: ignore[reportPrivateUsage]
        )
        process = live_handle.last_process
        assert process is not None
        assert session.transport_failed is not None
        assert not session.transport_failed.is_set()

        # The sandbox pauses: the stream ends, nobody writes anything.
        process.simulate_exit(0)
        for _ in range(50):
            if session.transport_failed.is_set():
                break
            await asyncio.sleep(0.01)

        assert session.transport_failed.is_set(), (
            "the watcher must declare the transport dead on its own. "
            "Waiting for a write means the writing request is already "
            "lost, which is the whole problem"
        )
        assert not process.stdin_buffer, (
            "nothing may be written to a frozen process"
        )


@pytest.mark.asyncio
async def test_session_wake_kill_failure_still_withholds_the_frame() -> None:
    """A refused kill is non-fatal but must not let the frame through.

    If E2B refuses the kill (API blip, process already reaped), the
    worst case is one resident process inside a sandbox. What must NOT
    happen is the pending frame reaching that frozen process, which
    would hand it to dead pooled sockets.

    The assertion is deliberately NOT ``transport_failed.is_set()``:
    the watcher sets that the instant the stream ends, before any
    frame is pushed, so it holds no matter what this branch does.
    Review caught the earlier version of this test asserting exactly
    that and therefore guarding nothing.
    """
    from typing import cast

    from mcp import types as mcp_types
    from mcp.shared.message import SessionMessage

    service, mock = make_e2b_service()
    mock.kill_command_error = E2BSDKError("SandboxError", "kill refused")
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="retire-kill-fails",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        write_stream = session.write_stream
        live_handle = cast(
            MockE2BSandboxHandle,
            service._live_sandboxes["retire-kill-fails"],  # type: ignore[reportPrivateUsage]
        )
        original_process = live_handle.last_process
        assert original_process is not None
        original_process.simulate_exit(0)
        for _ in range(5):
            await asyncio.sleep(0)

        await write_stream.send(SessionMessage(
            message=mcp_types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }),
        ))

        for _ in range(50):
            if mock.kill_commands:
                break
            await asyncio.sleep(0.01)
        assert mock.kill_commands, (
            "the kill must still be attempted even though it fails"
        )
        assert not original_process.stdin_buffer, (
            "a refused kill must not let the frame reach the frozen "
            "process"
        )


@pytest.mark.asyncio
async def test_session_reattach_failure_surfaces_on_read_stream() -> None:
    """If reattach itself fails (sandbox truly gone, network
    permanently broken), the pump must fail the transport — close the
    read side and set ``transport_failed`` — same shape as the
    ``stdin.send_failed`` path. Otherwise the in-flight tool call
    hangs forever waiting for stdout.
    """
    from typing import cast

    from mcp import types as mcp_types
    from mcp.shared.message import SessionMessage

    service, _ = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="reattach-fail-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        read_stream, write_stream = session.read_stream, session.write_stream
        live_handle = cast(
            MockE2BSandboxHandle,
            service._live_sandboxes["reattach-fail-session"],  # type: ignore[reportPrivateUsage]
        )
        original_process = live_handle.last_process
        assert original_process is not None

        # Make connect_command raise a wrapped SDK error.
        async def _failing_connect_command(
            *, pid: int, on_stdout: object, on_stderr: object,
        ) -> MockE2BProcessHandle:
            del pid, on_stdout, on_stderr
            raise E2BSDKError(
                "E2BNotFoundError", "sandbox sbx-0 not found",
            )

        live_handle.connect_command = _failing_connect_command  # type: ignore[reportAttributeAccessIssue]

        original_process.simulate_exit(0)
        for _ in range(5):
            await asyncio.sleep(0)

        request = SessionMessage(
            message=mcp_types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            }),
        )
        await write_stream.send(request)

        with pytest.raises(anyio.EndOfStream):
            await asyncio.wait_for(read_stream.receive(), timeout=2.0)
        assert session.transport_failed is not None
        assert session.transport_failed.is_set()


@pytest.mark.asyncio
async def test_session_surfaces_parse_errors_on_read_stream() -> None:
    """Garbage bytes on stdout become exceptions on the read stream,
    not crashed sessions — same shape as remote_stdio_client."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="contract-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ) as session:
        read_stream = session.read_stream
        cb = mock.last_on_stdout
        assert cb is not None
        feeder = await _invoke_cb_concurrently(cb, b"not-valid-json\n")
        received = await asyncio.wait_for(read_stream.receive(), timeout=2.0)
        await asyncio.wait_for(feeder, timeout=2.0)
        assert isinstance(received, Exception)


# ---------- reuse-on-restart (boot reconnect) ----------


@pytest.mark.asyncio
async def test_reconnect_falls_back_on_pre_feature_ref() -> None:
    """A persisted ref missing ``pid`` (paused-only or pre-feature
    write) is unreusable. The reconnect path must fall through to a
    fresh create — and the new ref must be repopulated with the
    ``(sandbox_id, pid)`` pair so the NEXT restart actually reuses.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-prefeature", command="npx")

    # Pre-seed a ref with sandbox_id but no pid — the unreusable
    # shape the reconnect path must reject.
    await persistence.upsert(
        SandboxPersistedRef(
            provider="e2b",
            org_id="acme",
            upstream_id=upstream.id,
            mcpolis_instance="legacy-instance",
            sandbox_id="legacy-sbx-1",
            paused_snapshot_id=None,
            pid=None,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime.now(UTC),
        ),
    )

    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service.session(
        session_id="prefeature-session", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Simulate the legitimate boot-reconnect lifecycle: the
        # session ends because the backend went down for a deploy
        # (lifespan handler marks every active session
        # preserve-on-close so sandboxes survive for next boot's
        # reattach). Without this, the new finally-block default is
        # to kill + delete the ref — which is correct for user Stop
        # but wrong for the boot-reconnect scenario this test pins.
        service.mark_session_preserve_on_close("prefeature-session")

    # Fall-through to fresh create: no reconnect attempt, one new
    # sandbox created.
    assert len(mock.connects) == 0, (
        "pre-feature ref should NOT trigger connect_sandbox"
    )
    assert len(mock.creates) == 1, (
        f"pre-feature ref should fall through to fresh create; "
        f"creates={mock.creates}"
    )

    # And critically: the ref was rewritten with the full pair,
    # so the NEXT restart will actually reuse.
    ref = await persistence.get(org_id="acme", upstream_id=upstream.id)
    assert ref is not None
    assert ref.sandbox_id is not None and ref.sandbox_id != "legacy-sbx-1", (
        "ref should now point at the freshly-created sandbox"
    )
    assert ref.pid is not None, "ref must now carry a pid"


@pytest.mark.asyncio
async def test_reconnect_recovers_from_externally_killed_sandbox() -> None:
    """The "Ready" badge promises a working tool call. With option-A
    loose semantics, deferred-attach trusts the cache; if the
    sandbox got externally killed (E2B GC, account ops, kill from
    another instance) between mcpolis shutdown and the next user
    request, the lazy reattach must fall back to fresh-create
    transparently — the user sees a longer first call, NOT an
    error.

    Setup mirrors the prod state where the cache says "ready" but
    the real sandbox is gone:
      - Persisted ref carries a sandbox_id pointing at a sandbox
        the mock has *never seen* (= killed externally).
      - cached_server_info populated (the upstream WAS ready
        before).

    Asserts ``connect_sandbox`` raises ``E2BNotFoundError`` and the
    service falls through to a fresh ``create_sandbox`` instead of
    propagating the error.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-killed", command="npx")

    await persistence.upsert(
        SandboxPersistedRef(
            provider="e2b",
            org_id="acme",
            upstream_id=upstream.id,
            mcpolis_instance="prior-instance",
            sandbox_id="sbx-killed-by-e2b",
            paused_snapshot_id=None,
            pid=4242,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime.now(UTC),
        ),
    )

    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    # Mock has no record of the sandbox in either ``live_infos`` or
    # ``_snapshot_metadata`` — ``connect_sandbox`` will raise.

    async with service.session(
        session_id="killed-recovery", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Mark preserve so the fresh-created replacement ref isn't
        # wiped on session exit — the test asserts the ref points at
        # the new sandbox after the boot-reconnect lifecycle.
        service.mark_session_preserve_on_close("killed-recovery")

    # Reconnect must fall through to a fresh create — propagating
    # the not-found error to the user would mean the "Ready" badge
    # was a lie. The service-level proof is: a fresh ``create_sandbox``
    # ran, AND the persistence ref now points at the new id.
    # (We can't assert on ``mock.connects`` because the mock raises
    # ``E2BNotFoundError`` *before* appending — but the service-side
    # log line ``sandbox.e2b.reconnect.connect_sandbox_failed`` fires,
    # which is the trigger for the fresh-create fallback.)
    assert len(mock.creates) == 1, (
        f"expected one fresh-create after the reconnect failed; "
        f"got creates={mock.creates}"
    )
    ref = await persistence.get(org_id="acme", upstream_id=upstream.id)
    assert ref is not None and ref.sandbox_id is not None
    assert ref.sandbox_id != "sbx-killed-by-e2b", (
        "ref should now point at the freshly-created sandbox, not "
        "the dead one"
    )


@pytest.mark.asyncio
async def test_reconnect_recovers_from_dead_mcp_process() -> None:
    """Same loose-semantics promise, different failure mode: the
    persisted sandbox is alive on E2B but the MCP subprocess inside
    has died (the bogus pattern after a successful run, or any
    crash). ``connect_command(pid)`` raises NotFoundException; the
    service must kill the (now-stuck) sandbox and fresh-create a
    replacement so the user's first tool call still works.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-dead-pid", command="npx")

    # First, run a warmup session so the mock's ``live_infos`` has
    # a real sandbox_id we can target. The reconnect on the SECOND
    # session attempt is what we're testing.
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service.session(
        session_id="warmup", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Simulate "backend went down for deploy" — the warmup ref
        # must survive the session exit so the second session below
        # can simulate the boot-reconnect attempt against it.
        service.mark_session_preserve_on_close("warmup")
    ref_after_warmup = await persistence.get(
        org_id="acme", upstream_id=upstream.id,
    )
    assert ref_after_warmup is not None
    alive_sandbox_id = ref_after_warmup.sandbox_id
    assert alive_sandbox_id is not None

    # Now corrupt the ref's pid — mirrors "MCP subprocess died after
    # mcpolis shut down." Sandbox is alive on E2B; the pid is gone.
    await persistence.upsert(
        ref_after_warmup.model_copy(update={"pid": 999_999_999}),
    )

    # And rig the mock's next connect_command to raise NotFoundError
    # for THAT pid. The simplest path: monkey-patch the handle that
    # ``connect_sandbox`` returns by intercepting the next handle's
    # ``connect_command``.
    creates_before = len(mock.creates)
    kills_before = len(mock.kills)

    original_connect = mock.connect_sandbox

    async def _connect_with_broken_respawn(snapshot_id: str):  # type: ignore[no-untyped-def]
        handle = await original_connect(snapshot_id)

        # The reconnect path no longer reattaches to the recorded pid;
        # it kills it and respawns. The equivalent "sandbox is alive
        # but unusable" failure is therefore a failing ``run_command``.
        async def _raise_not_found(*_args: Any, **_kwargs: Any) -> None:
            raise E2BNotFoundError(
                "E2BNotFoundError",
                "cannot spawn in sandbox",
            )

        handle.run_command = _raise_not_found  # type: ignore[method-assign,assignment]
        return handle

    mock.connect_sandbox = _connect_with_broken_respawn  # type: ignore[method-assign,assignment]

    # Lazy reattach simulating first user call after restart.
    async with service.session(
        session_id="dead-pid-recovery", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Mark preserve so the fresh-created replacement ref survives
        # for the post-session assertion that the ref now points at
        # the new sandbox.
        service.mark_session_preserve_on_close("dead-pid-recovery")

    # The service should have killed the stuck sandbox and fresh-
    # created a replacement so the user's tool call works.
    assert len(mock.kills) > kills_before, (
        "expected the stuck sandbox to be killed after the respawn "
        f"failed; kills={mock.kills}, creates={mock.creates}"
    )
    assert len(mock.creates) == creates_before + 1, (
        "expected one fresh-create after the respawn "
        f"failure; creates={mock.creates}"
    )
    ref = await persistence.get(org_id="acme", upstream_id=upstream.id)
    assert ref is not None and ref.sandbox_id is not None
    assert ref.sandbox_id != alive_sandbox_id


@pytest.mark.asyncio
async def test_reconnect_respawn_failed_log_shape() -> None:
    """The ``sandbox.e2b.reconnect.respawn_failed`` log line is
    the operator's only signal that a reconnect attempt failed
    before falling back to a fresh create. The shape MUST carry the four operator-actionable fields
    or on-call dashboards lose the ability to:

    - filter by severity (``warning`` — this is a user-visible 60s
      delay, not routine);
    - size the slowness without diffing timestamps (``elapsed_ms``);
    - split timeout vs other E2BSDKError causes without grepping
      the freeform error string (``error_class``);
    - tell whether the upstream is broken or self-healing from one
      log line in isolation (``fallback="fresh_create"``).

    Documented in ``internal/documents/metrics-to-monitor.md``. This test is
    the regression gate for that doc.
    """
    from structlog.testing import capture_logs

    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-log-shape", command="npx")

    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service.session(
        session_id="warmup", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Preserve so the second session below has a live ref to try
        # reconnecting against (the failure path under test).
        service.mark_session_preserve_on_close("warmup")

    # Rig the next reattach so connect_command raises a NotFoundError —
    # the same shape the dead-pid recovery test exercises, except here
    # we only care about the LOG output.
    original_connect = mock.connect_sandbox

    async def _connect_with_broken_respawn(snapshot_id: str):  # type: ignore[no-untyped-def]
        handle = await original_connect(snapshot_id)

        async def _raise_not_found(*_args: Any, **_kwargs: Any) -> None:
            raise E2BNotFoundError(
                "E2BNotFoundError",
                "cannot spawn in sandbox",
            )

        handle.run_command = _raise_not_found  # type: ignore[method-assign,assignment]
        return handle

    mock.connect_sandbox = _connect_with_broken_respawn  # type: ignore[method-assign,assignment]

    with capture_logs() as logs:
        async with service.session(
            session_id="log-shape-recovery", org_id="acme",
            upstream=upstream, resources=make_default_resources(),
            denylist=(),
        ):
            pass

    failed_events = [
        le for le in logs
        if le.get("event") == "sandbox.e2b.reconnect.respawn_failed"
    ]
    assert len(failed_events) == 1, (
        f"expected exactly one respawn_failed event, "
        f"got {len(failed_events)}: {logs}"
    )
    evt = failed_events[0]
    assert evt["log_level"] == "warning", (
        f"respawn_failed must be ``warning`` so on-call "
        f"severity filters surface it; got {evt['log_level']!r}"
    )
    assert evt["org_id"] == "acme"
    assert evt["upstream_id"] == "ups-log-shape"
    assert evt["error_class"] == "E2BNotFoundError", (
        f"error_class lets dashboards split timeout vs other E2BSDKError "
        f"causes without grepping the freeform error string; "
        f"got {evt.get('error_class')!r}"
    )
    assert evt["fallback"] == "fresh_create", (
        f"fallback signals the self-healing handoff explicitly so a "
        f"single log line carries the full story; got {evt.get('fallback')!r}"
    )
    assert isinstance(evt.get("elapsed_ms"), float), (
        f"elapsed_ms quantifies the reconnect delay (1ms vs 60s); "
        f"got {type(evt.get('elapsed_ms'))}"
    )
    assert evt["elapsed_ms"] >= 0
    assert "error" in evt, "freeform error string preserved"
    # ``stale_pid``, not ``pid``: the field names the process we retired,
    # and there is no live pid at this point — the respawn that would
    # have produced one is what just failed.
    assert "sandbox_id" in evt and "stale_pid" in evt


@pytest.mark.asyncio
async def test_wipe_for_fresh_restart_kills_and_clears() -> None:
    """``MCPOLIS_E2B_FRESH_SANDBOXES`` operator override: kills
    every persisted sandbox + clears every ref."""
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-wipe", command="npx")

    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service.session(
        session_id="wipe-session", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        # Preserve so the ref + sandbox survive the session exit;
        # the test then verifies wipe_for_fresh_restart kills + clears
        # them. Without this, the new finally-block default kills
        # the sandbox + deletes the ref, leaving nothing to wipe.
        service.mark_session_preserve_on_close("wipe-session")
    assert await persistence.get(org_id="acme", upstream_id="ups-wipe") is not None
    kills_before = len(mock.kills)

    cleared = await service.wipe_for_fresh_restart()

    assert cleared == 1
    assert await persistence.get(org_id="acme", upstream_id="ups-wipe") is None
    assert len(mock.kills) == kills_before + 1, (
        "wipe should kill the persisted sandbox"
    )


# ---------- map_exit ----------


def test_map_exit_auth_error_translates_to_auth_failed() -> None:
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        error_class="E2BAuthError", raw_message="invalid api key",
    )
    reason, detail = service.map_exit(info)
    assert reason is ExitReason.AUTH_FAILED
    assert detail == "invalid api key"


def test_map_exit_quota_error_translates_to_account_limit() -> None:
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        error_class="E2BQuotaError", raw_message="monthly cap reached",
    )
    reason, detail = service.map_exit(info)
    assert reason is ExitReason.ACCOUNT_LIMIT_EXCEEDED
    assert detail == "monthly cap reached"


def test_map_exit_rate_limit_translates_to_account_limit() -> None:
    """SDK's RateLimitException is the rate-limited variant of the
    quota error."""
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        error_class="RateLimitException", raw_message="429 received",
    )
    reason, _ = service.map_exit(info)
    assert reason is ExitReason.ACCOUNT_LIMIT_EXCEEDED


def test_map_exit_subprocess_exit_distinct_from_provider_error() -> None:
    """A non-zero exit code from the MCP process itself surfaces as
    SUBPROCESS_EXITED — admins can tell "my MCP crashed" apart from
    "the provider failed"."""
    service, _ = make_e2b_service()
    info = ProviderExitInfo(exit_code=42, raw_message="exited with 42")
    reason, _ = service.map_exit(info)
    assert reason is ExitReason.SUBPROCESS_EXITED


def test_map_exit_uncategorized_falls_back_to_provider_error() -> None:
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        error_class="SomeFutureSDKError", raw_message="exotic boom",
    )
    reason, detail = service.map_exit(info)
    assert reason is ExitReason.PROVIDER_ERROR
    assert detail == "exotic boom"


def test_map_exit_empty_input() -> None:
    service, _ = make_e2b_service()
    reason, _ = service.map_exit(ProviderExitInfo())
    assert reason is ExitReason.PROVIDER_ERROR


# ---------- pause / resume ----------


@pytest.mark.asyncio
async def test_pause_unknown_session_returns_none() -> None:
    """No live session registered ⇒ pause is a no-op. Same shape
    as the cross-backend contract: a stale session-id (e.g. idle
    reaper firing after exit) doesn't crash."""
    service, _ = make_e2b_service()
    assert await service.pause(session_id="never-opened") is None


@pytest.mark.asyncio
async def test_pause_active_session_returns_snapshot_ref() -> None:
    """pause(session_id) for a live registered session calls
    handle.pause() on the underlying SDK and returns a
    well-formed ``SnapshotRef``."""
    from mcpolis.domain.services.sandbox_service import SnapshotRef

    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="live-session-1",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        ref = await service.pause(session_id="live-session-1")
        assert isinstance(ref, SnapshotRef)
        assert ref.provider == "e2b"
        assert ref.snapshot_id.startswith("snap-")
        # Pause renders the handle dead — a second pause call
        # safely returns None instead of double-pausing.
        assert await service.pause(session_id="live-session-1") is None
    # After context exit the SDK's view of the world reflects
    # the pause: the original sandbox is in ``paused`` state.
    paused = [info for info in mock.live_infos if info.state == "paused"]
    assert len(paused) == 1


@pytest.mark.asyncio
async def test_pause_then_resume_round_trip() -> None:
    """The classic resume flow: open a session, pause it, then open
    a fresh session passing ``resume_from=ref`` — the SDK
    sees ``Sandbox.connect(snapshot_id)`` instead of
    ``Sandbox.create``."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    async with service.session(
        session_id="live-session-1",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        ref = await service.pause(session_id="live-session-1")
    assert ref is not None
    creates_before = len(mock.creates)
    async with service.session(
        session_id="live-session-2",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
        resume_from=ref,
    ):
        pass
    # Resume → connect, not create.
    assert len(mock.creates) == creates_before
    assert len(mock.connects) == 1
    assert mock.connects[0].snapshot_id == ref.snapshot_id


@pytest.mark.asyncio
async def test_capabilities_declare_pause_supported() -> None:
    """E2B advertises pause/resume support — the admin UI uses this
    flag to decide whether to surface the "pause" control."""
    service, _ = make_e2b_service()
    assert service.capabilities().supports_pause_resume is True


# ---------- error mapping at SDK boundary ----------


@pytest.mark.asyncio
async def test_create_failure_propagates_through_session() -> None:
    service, mock = make_e2b_service()
    mock.create_raises = E2BAuthError(
        "E2BAuthError", "test-bad-key",
    )
    upstream = make_upstream_definition(id="ups-x", command="npx")
    with pytest.raises(E2BSDKError):
        async with service.session(
            session_id="contract-session",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            pass


@pytest.mark.asyncio
async def test_quota_failure_propagates_through_session() -> None:
    service, mock = make_e2b_service()
    mock.create_raises = E2BQuotaError(
        "E2BQuotaError", "cap reached",
    )
    upstream = make_upstream_definition(id="ups-x", command="npx")
    with pytest.raises(E2BSDKError):
        async with service.session(
            session_id="contract-session",
            org_id="acme",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            pass


# ---------- E2B Volumes (persistent_disk_enabled) ----------


def test_capabilities_supports_persistent_disk_only_with_persistence() -> None:
    """The capability flag tracks whether the service can actually
    round-trip a volume id across sessions. Without a persistence
    repo it would create a fresh volume each session — pointless —
    so the flag stays False.
    """
    service_no_persist, _ = make_e2b_service(persistence=None)
    assert service_no_persist.capabilities().supports_persistent_disk is False

    service_with_persist, _ = make_e2b_service(
        persistence=InMemorySandboxPersistenceRepository(),
    )
    assert service_with_persist.capabilities().supports_persistent_disk is True


def test_capabilities_persistent_disk_false_when_volumes_disabled() -> None:
    """The operator gate ``volumes_enabled=False`` keeps the
    capability flag False even with a persistence repo wired up —
    matches the case where the E2B account hasn't yet had Volumes
    enabled in the dashboard, so the SDK would 403 on first use.
    """
    service, _ = make_e2b_service(
        persistence=InMemorySandboxPersistenceRepository(),
        volumes_enabled=False,
    )
    assert service.capabilities().supports_persistent_disk is False


@pytest.mark.asyncio
async def test_session_skips_volume_when_operator_gate_disabled() -> None:
    """Even if an upstream has persistent_disk_enabled=True, the
    backend-level ``volumes_enabled=False`` gate causes the session
    to boot ephemerally without touching the volume API. A warning
    is logged so the operator can spot the mismatch.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(
        persistence=persistence, volumes_enabled=False,
    )
    upstream = make_persistent_disk_upstream(id="ups-vol")
    async with service.session(
        session_id="s",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass

    # No volume API calls, no volume_mounts on create.
    assert mock.volume_creates == []
    assert mock.creates[0].volume_mounts is None


@pytest.mark.asyncio
async def test_session_skips_volume_when_persistent_disk_disabled() -> None:
    """An upstream with persistent_disk_enabled=False (the default)
    creates a sandbox with no volume_mounts; the volume API is never
    touched.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_upstream_definition(id="ups-no-vol", command="npx")
    async with service.session(
        session_id="s",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass

    assert mock.creates[0].volume_mounts is None
    assert mock.volume_creates == []


@pytest.mark.asyncio
async def test_session_creates_volume_on_first_persistent_session() -> None:
    """First session with persistent_disk_enabled=True provisions a
    fresh volume via create_volume, persists the id, and mounts it
    at PERSISTENT_VOLUME_MOUNT_PATH.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_persistent_disk_upstream(id="ups-vol")
    async with service.session(
        session_id="s1",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass

    assert len(mock.volume_creates) == 1
    created = mock.volume_creates[0]
    assert "acme" in created.name
    assert "ups-vol" in created.name
    assert mock.creates[0].volume_mounts == {
        PERSISTENT_VOLUME_MOUNT_PATH: created.volume_id,
    }
    persisted = await persistence.get(org_id="acme", upstream_id="ups-vol")
    assert persisted is not None
    assert persisted.metadata[VOLUME_METADATA_KEY] == created.volume_id


@pytest.mark.asyncio
async def test_session_reuses_existing_volume_on_subsequent_sessions() -> None:
    """Once a volume is provisioned, every subsequent session for the
    same (org, upstream) reuses the same volume_id rather than
    creating a new one. That's the whole point of persistence.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_persistent_disk_upstream(id="ups-vol")

    async with service.session(
        session_id="s1", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass
    async with service.session(
        session_id="s2", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass
    async with service.session(
        session_id="s3", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass

    # Exactly one volume provisioning across three sessions.
    assert len(mock.volume_creates) == 1
    expected_id = mock.volume_creates[0].volume_id
    for create in mock.creates:
        assert create.volume_mounts == {
            PERSISTENT_VOLUME_MOUNT_PATH: expected_id,
        }


@pytest.mark.asyncio
async def test_on_upstream_removed_destroys_volume_and_clears_ref() -> None:
    """Operator delete: volume gets destroyed via the SDK, persistence
    ref is cleared so the reconciler doesn't see a phantom mapping.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_persistent_disk_upstream(id="ups-vol")
    async with service.session(
        session_id="s1", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass
    volume_id = mock.volume_creates[0].volume_id

    await service.on_upstream_removed(org_id="acme", upstream_id="ups-vol")

    assert [d.volume_id for d in mock.volume_destroys] == [volume_id]
    assert (
        await persistence.get(org_id="acme", upstream_id="ups-vol")
    ) is None


@pytest.mark.asyncio
async def test_on_upstream_removed_idempotent_when_no_ref() -> None:
    """Calling teardown for an upstream that never opted in (or has
    already been cleaned up) is a safe no-op — never raises, never
    touches the volume API.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)

    await service.on_upstream_removed(org_id="acme", upstream_id="never-opened")
    await service.on_upstream_removed(org_id="acme", upstream_id="never-opened")

    assert mock.volume_destroys == []


@pytest.mark.asyncio
async def test_on_upstream_removed_handles_already_deleted_volume() -> None:
    """If the volume has already been destroyed out-of-band (operator
    deleted it from the E2B dashboard), the SDK's NotFound is treated
    as a successful teardown so the persistence ref still gets cleared
    and a re-run of the operator action becomes a clean no-op.
    """
    from mcpolis.adapters.sandbox_e2b.client import E2BNotFoundError

    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_persistent_disk_upstream(id="ups-vol")
    async with service.session(
        session_id="s1", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass
    mock.volume_destroy_raises = E2BNotFoundError(
        "E2BNotFoundError", "volume already gone",
    )

    await service.on_upstream_removed(org_id="acme", upstream_id="ups-vol")

    assert (
        await persistence.get(org_id="acme", upstream_id="ups-vol")
    ) is None


@pytest.mark.asyncio
async def test_on_upstream_removed_keeps_ref_on_transient_destroy_error() -> None:
    """If volume destroy raises something other than NotFound (network
    blip, rate limit, etc.) we MUST leave the persistence ref intact
    so a retry can complete the teardown. Clearing the ref + leaving
    the volume orphaned would be worse than leaving the ref so the
    reconciler can chase it later.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    upstream = make_persistent_disk_upstream(id="ups-vol")
    async with service.session(
        session_id="s1", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        pass
    mock.volume_destroy_raises = E2BSDKError(
        "E2BSDKError", "transient network",
    )

    await service.on_upstream_removed(org_id="acme", upstream_id="ups-vol")

    persisted = await persistence.get(
        org_id="acme", upstream_id="ups-vol",
    )
    assert persisted is not None
    assert VOLUME_METADATA_KEY in persisted.metadata


# ---------- kill-on-stop + preserve-on-shutdown contract ----------
#
# These tests pin the central change: a clean session exit kills the
# sandbox by default (so user Stop / Delete / idle disconnect don't
# leak paused storage cost), and only the explicit
# ``mark_session_preserve_on_close`` hook (called by the lifespan
# handler on graceful shutdown) leaves the sandbox alive for the next
# boot's reconnect.


@pytest.mark.asyncio
async def test_session_close_kills_sandbox_by_default() -> None:
    """The default close path MUST kill the sandbox even when
    reuse-on-restart + persistence are wired. Replaces the old
    ``test_session_skips_kill_when_reuse_on_restart_enabled`` (whose
    assertion is now inverted: paused sandboxes accrue cost so we
    don't want them lingering once the user signals they're done).
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    upstream = make_upstream_definition(id="ups-kill", command="npx")
    async with service.session(
        session_id="kill-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert len(mock.kills) == 1, (
        f"clean session exit should kill the sandbox; mock.kills={mock.kills}"
    )


@pytest.mark.asyncio
async def test_session_close_deletes_persisted_ref_when_killing() -> None:
    """When the finally-block kills the sandbox, the persisted ref
    must also be deleted — otherwise the next boot wastes an E2B
    connect_sandbox API call against a dead id before falling
    through to fresh-create."""
    persistence = InMemorySandboxPersistenceRepository()
    service, _ = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    upstream = make_upstream_definition(id="ups-cleanup", command="npx")
    async with service.session(
        session_id="cleanup-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        pass
    ref = await persistence.get(org_id="acme", upstream_id="ups-cleanup")
    assert ref is None, (
        f"kill path must delete the now-stale ref; got ref={ref}"
    )


@pytest.mark.asyncio
async def test_session_close_preserves_sandbox_when_marked() -> None:
    """The lifespan handler marks every active session
    preserve-on-close before tearing down on graceful shutdown.
    Sandboxes flagged this way must skip the kill+delete path so the
    next boot's ``_try_reconnect`` finds them.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    upstream = make_upstream_definition(id="ups-preserve", command="npx")
    async with service.session(
        session_id="preserve-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        service.mark_session_preserve_on_close("preserve-session")
    assert len(mock.kills) == 0, (
        f"preserve-marked session must skip kill; mock.kills={mock.kills}"
    )
    ref = await persistence.get(org_id="acme", upstream_id="ups-preserve")
    assert ref is not None, "preserve-marked session must keep the ref"
    assert ref.sandbox_id is not None
    assert ref.pid is not None


@pytest.mark.asyncio
async def test_explicit_pause_path_still_skips_kill() -> None:
    """Regression guard for Path 1 (the explicit
    ``SandboxService.pause(session_id)`` admin flow). Calling
    ``pause`` mid-session must still skip the destructive cleanup
    so the snapshot is the new state — independent of the
    kill-on-close default we just introduced.
    """
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-explicit-pause", command="npx")
    async with service.session(
        session_id="explicit-pause-session",
        org_id="acme",
        upstream=upstream,
        resources=make_default_resources(),
        denylist=(),
    ):
        snapshot = await service.pause("explicit-pause-session")
        assert snapshot is not None
    assert len(mock.kills) == 0, (
        f"explicit pause MUST skip kill; mock.kills={mock.kills}"
    )
    # The snapshot must remain alive on the provider — i.e. there's
    # at least one paused sandbox (the one we just paused) and we
    # didn't accidentally kill it during teardown.
    paused = [info for info in mock.live_infos if info.state == "paused"]
    assert len(paused) == 1, (
        f"explicit pause should leave exactly one paused sandbox alive; "
        f"live_infos={mock.live_infos}"
    )


@pytest.mark.asyncio
async def test_try_reconnect_succeeds_with_drifted_config() -> None:
    """Boot reconnect contract: a config edit on disk does NOT take
    effect until the user explicitly Stop+Restart. So when the live
    upstream config differs from what was used when the persisted
    sandbox was created, the reconnect must still reattach — NOT
    silently apply the pending edit by killing + fresh-creating.
    """
    persistence = InMemorySandboxPersistenceRepository()

    # Service A: writes the persistence ref with one configuration.
    upstream_v1 = make_upstream_definition(id="ups-drift", command="npx")
    upstream_v1.stdio.args = ["-y", "package-v1"]  # type: ignore[union-attr]
    upstream_v1.stdio.env = {"FEATURE_FLAG": "off"}  # type: ignore[union-attr]
    service_a, mock = make_e2b_service(persistence=persistence)
    service_a._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service_a.session(
        session_id="drift-warmup", org_id="acme",
        upstream=upstream_v1, resources=make_default_resources(),
        denylist=(),
        extra_env={"MCP_AUTH_TOKEN": "old"},
    ):
        # Simulate the deploy that ends the warmup session — sandbox
        # must survive for the boot-reconnect under test below.
        service_a.mark_session_preserve_on_close("drift-warmup")
    creates_after_warmup = len(mock.creates)
    kills_after_warmup = len(mock.kills)

    # Service B: same persistence, same upstream id, but the live
    # upstream definition has DIFFERENT args + env (a config edit
    # made while service A was down). The reconnect must still
    # reattach — no kill, no fresh-create.
    upstream_v2 = make_upstream_definition(id="ups-drift", command="npx")
    upstream_v2.stdio.args = ["-y", "package-v2"]  # type: ignore[union-attr]
    upstream_v2.stdio.env = {"FEATURE_FLAG": "on"}  # type: ignore[union-attr]
    service_b = E2BSandboxService(
        mock,
        mcpolis_instance="test-instance-b",
        on_timeout_seconds=60,
        persistence=persistence,
        volumes_enabled=True,
        reuse_sandboxes_on_restart=True,
    )
    async with service_b.session(
        session_id="drift-reattach", org_id="acme",
        upstream=upstream_v2, resources=make_default_resources(),
        denylist=(),
        extra_env={"MCP_AUTH_TOKEN": "new"},  # also drifted
    ):
        service_b.mark_session_preserve_on_close("drift-reattach")

    assert len(mock.creates) == creates_after_warmup, (
        "drift on boot reconnect MUST NOT trigger fresh-create — "
        "config edits propagate only on user Stop+Restart"
    )
    assert len(mock.kills) == kills_after_warmup, (
        "drift on boot reconnect MUST NOT kill the sandbox — "
        f"got new kills: {mock.kills[kills_after_warmup:]}"
    )


@pytest.mark.asyncio
async def test_try_reconnect_falls_through_when_sandbox_dead() -> None:
    """The hash check is gone, but the dead-sandbox fallback in
    ``_try_reconnect`` must still work: if connect_sandbox raises,
    fresh-create kicks in.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-dead", command="npx")
    await persistence.upsert(
        SandboxPersistedRef(
            provider="e2b",
            org_id="acme",
            upstream_id=upstream.id,
            mcpolis_instance="prior",
            sandbox_id="sbx-already-dead",
            paused_snapshot_id=None,
            pid=4242,
            metadata={},
            cached_server_info=None,
            cached_self_description=None,
            last_updated=datetime.now(UTC),
        ),
    )
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    # Mock has no record of "sbx-already-dead" → connect_sandbox raises.
    async with service.session(
        session_id="dead-fallback", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        pass
    assert len(mock.creates) == 1, (
        f"dead-sandbox reconnect must fall through to fresh-create; "
        f"creates={mock.creates}"
    )


def test_persistence_ref_does_not_carry_config_hash() -> None:
    """Compile-time guard: the ``config_hash`` field is removed from
    ``SandboxPersistedRef``. Re-adding it would be load-bearing in
    Mongo, so this test pins the absence so the next person who
    types ``config_hash`` into the model gets a red CI."""
    ref = SandboxPersistedRef(
        provider="e2b",
        org_id="acme",
        upstream_id="ups-shape",
        mcpolis_instance="i",
        sandbox_id="sbx-1",
        paused_snapshot_id=None,
        pid=1,
        metadata={},
        cached_server_info=None,
        cached_self_description=None,
        last_updated=datetime.now(UTC),
    )
    assert not hasattr(ref, "config_hash"), (
        "SandboxPersistedRef.config_hash was removed in the kill-on-stop "
        "cleanup; re-adding it would resurrect the silent-config-drift "
        "footgun."
    )


@pytest.mark.asyncio
async def test_active_session_ids_lists_only_live_sessions() -> None:
    """The lifespan shutdown hook iterates ``active_session_ids()``
    to mark every live session preserve-on-close. The set must
    include sessions currently inside their context manager and
    NOT include sessions that have already exited.
    """
    service, _ = make_e2b_service()
    upstream = make_upstream_definition(id="ups-active", command="npx")
    assert service.active_session_ids() == []
    async with service.session(
        session_id="active-1", org_id="acme",
        upstream=upstream, resources=make_default_resources(),
        denylist=(),
    ):
        async with service.session(
            session_id="active-2", org_id="acme",
            upstream=upstream, resources=make_default_resources(),
            denylist=(),
        ):
            inside = set(service.active_session_ids())
            assert inside == {"active-1", "active-2"}
        # active-2 exited; only active-1 remains.
        assert set(service.active_session_ids()) == {"active-1"}
    assert service.active_session_ids() == []


@pytest.mark.asyncio
async def test_restart_after_stop_uses_updated_config() -> None:
    """The contract that motivated this whole change: when a user
    edits the upstream JSON, clicks Stop, then clicks Restart, the
    new config (args + env) MUST be the one the spawned MCP process
    sees. Before the kill-on-stop change, this scenario worked but
    via the (now removed) config-hash drift gate at boot reconnect.
    Now it works via the more direct path: Stop kills the sandbox,
    Restart fresh-creates with the live config.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]

    # User connects with config A (initial install).
    upstream_v1 = make_upstream_definition(id="ups-restart", command="npx")
    upstream_v1.stdio.args = ["-y", "package@1.0.0"]  # type: ignore[union-attr]
    upstream_v1.stdio.env = {"FEATURE_FLAG": "off"}  # type: ignore[union-attr]
    async with service.session(
        session_id="restart-1", org_id="acme",
        upstream=upstream_v1, resources=make_default_resources(),
        denylist=(),
        extra_env={"MCP_AUTH_TOKEN": "tok-a"},
    ):
        pass  # User clicks Stop — finally-block kills + deletes ref.

    # Verify Stop ran the kill+delete path (precondition for Restart
    # to fresh-create with updated config rather than reattach).
    assert len(mock.kills) == 1
    assert await persistence.get(
        org_id="acme", upstream_id="ups-restart",
    ) is None

    # User edits the JSON, then clicks Restart with the UPDATED
    # upstream definition (different args + env).
    upstream_v2 = make_upstream_definition(id="ups-restart", command="npx")
    upstream_v2.stdio.args = ["-y", "package@2.0.0"]  # type: ignore[union-attr]
    upstream_v2.stdio.env = {"FEATURE_FLAG": "on"}  # type: ignore[union-attr]
    async with service.session(
        session_id="restart-2", org_id="acme",
        upstream=upstream_v2, resources=make_default_resources(),
        denylist=(),
        extra_env={"MCP_AUTH_TOKEN": "tok-b"},
    ):
        pass

    # Two sandboxes were created (Stop killed the first, Restart
    # produced the second) — no reconnect attempt.
    assert len(mock.creates) == 2, (
        f"Stop+Restart should fresh-create on restart; "
        f"creates={mock.creates}"
    )
    assert len(mock.connects) == 0, (
        "no reconnect should be attempted (ref was deleted on Stop)"
    )

    # The second run_command MUST have received config B's argv + env.
    cmd_v1 = mock.commands[0]
    cmd_v2 = mock.commands[1]
    assert cmd_v1.argv == ["npx", "-y", "package@1.0.0"], (
        f"first session should run config A; got {cmd_v1.argv}"
    )
    assert cmd_v2.argv == ["npx", "-y", "package@2.0.0"], (
        f"restart MUST pick up new args; got {cmd_v2.argv}"
    )
    # Env propagation: extra_env + cfg.env must reach the spawned
    # process under the new config.
    assert cmd_v2.env.get("FEATURE_FLAG") == "on", (
        f"restart MUST pick up new cfg.env; got {cmd_v2.env}"
    )
    assert cmd_v2.env.get("MCP_AUTH_TOKEN") == "tok-b", (
        f"restart MUST pick up new extra_env; got {cmd_v2.env}"
    )


@pytest.mark.asyncio
async def test_mark_all_active_sessions_preserve_on_close() -> None:
    """The bulk variant the lifespan handler calls. Every currently-
    live session must be marked preserve, and the count returned
    matches what gets logged.
    """
    persistence = InMemorySandboxPersistenceRepository()
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    up_a = make_upstream_definition(id="ups-shutdown-a", command="npx")
    up_b = make_upstream_definition(id="ups-shutdown-b", command="npx")
    async with service.session(
        session_id="sd-a", org_id="acme",
        upstream=up_a, resources=make_default_resources(),
        denylist=(),
    ):
        async with service.session(
            session_id="sd-b", org_id="acme",
            upstream=up_b, resources=make_default_resources(),
            denylist=(),
        ):
            marked = service.mark_all_active_sessions_preserve_on_close()
            assert marked == 2
    # Both sessions exited; neither should have been killed because
    # they were marked preserve before exit.
    assert len(mock.kills) == 0, (
        f"both sessions were marked preserve-on-close; mock.kills={mock.kills}"
    )
    ref_a = await persistence.get(org_id="acme", upstream_id="ups-shutdown-a")
    ref_b = await persistence.get(org_id="acme", upstream_id="ups-shutdown-b")
    assert ref_a is not None and ref_b is not None


class _ScriptedDockerProc:
    """Process handle whose ``wait()`` returns a scripted exit code at once
    (unlike the standard mock, which blocks until ``simulate_exit``)."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


class _ScriptedDockerHandle:
    """Minimal sandbox-handle stand-in for ``_poll_docker_ready``: scripts the
    exit code of each ``docker info`` probe and counts how many were run."""

    def __init__(self, info_exit_codes: list[int]) -> None:
        self._codes = list(info_exit_codes)
        self.info_calls = 0

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> _ScriptedDockerProc:
        assert argv[:2] == ["docker", "info"]
        idx = self.info_calls
        self.info_calls += 1
        # Past the script: treat as "not ready" so the budget can be exhausted.
        code = self._codes[idx] if idx < len(self._codes) else 1
        return _ScriptedDockerProc(code)


@pytest.mark.asyncio
async def test_poll_docker_ready_requires_consecutive_successes() -> None:
    # A freshly-launched dockerd answers once, drops, then comes up for good.
    # Readiness must NOT trip on the first lone success (the old behavior, the
    # flaky-race bug) — it must wait for a stable run of consecutive successes.
    service, _client = make_e2b_service()
    handle = _ScriptedDockerHandle([0, 1, 0, 0, 0])

    attempts = await service._poll_docker_ready(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
        max_polls=10,
        poll_interval=0.0,
        required_consecutive=3,
    )

    # Streak: 0(1) →1(reset) →0(1) →0(2) →0(3, ready) = 5th probe.
    assert attempts == 5
    assert handle.info_calls == 5


@pytest.mark.asyncio
async def test_poll_docker_ready_returns_none_when_never_stable() -> None:
    service, _client = make_e2b_service()
    handle = _ScriptedDockerHandle([1, 1, 1, 1, 1])

    attempts = await service._poll_docker_ready(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
        max_polls=5,
        poll_interval=0.0,
        required_consecutive=3,
    )

    assert attempts is None
    assert handle.info_calls == 5


@pytest.mark.asyncio
async def test_poll_docker_ready_stable_from_the_start() -> None:
    service, _client = make_e2b_service()
    handle = _ScriptedDockerHandle([0, 0, 0])

    attempts = await service._poll_docker_ready(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
        max_polls=10,
        poll_interval=0.0,
        required_consecutive=3,
    )

    assert attempts == 3
    assert handle.info_calls == 3


class _ScriptedDaemonStartHandle:
    """Sandbox-handle stand-in for the full ``_start_docker_daemon`` flow.

    Classifies each ``run_command`` by its argv into a label, records the
    label sequence for assertions, and scripts the exit codes of the
    ``docker info`` probes and the boot-daemon-pending probe."""

    def __init__(
        self, *, info_exit_codes: list[int], pending_exit_code: int,
    ) -> None:
        self._info_codes = list(info_exit_codes)
        self._pending_code = pending_exit_code
        self.calls: list[str] = []

    def _classify(self, argv: list[str]) -> tuple[str, int]:
        joined = " ".join(argv)
        if argv[:2] == ["docker", "info"]:
            idx = sum(1 for c in self.calls if c == "info")
            code = (
                self._info_codes[idx]
                if idx < len(self._info_codes) else 1
            )
            return "info", code
        if "pgrep -x dockerd" in joined:
            return "pending_probe", self._pending_code
        if "systemctl stop" in joined:
            return "stop_engine", 0
        if "nohup sudo dockerd" in joined:
            return "launch", 0
        if argv[:2] == ["sudo", "chmod"]:
            return "chmod", 0
        if "[ -S /var/run/docker.sock ]" in joined:
            return "chmod_early", 0
        if argv[:1] == ["cat"]:
            return "read_log", 0
        raise AssertionError(f"unexpected command: {argv!r}")

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> _ScriptedDockerProc:
        label, code = self._classify(argv)
        self.calls.append(label)
        return _ScriptedDockerProc(code)


@pytest.mark.asyncio
async def test_start_docker_daemon_already_serving_skips_launch() -> None:
    # Daemon answers the very first probe (boot-managed dockerd is up):
    # nothing gets stopped or launched, only the socket perms are fixed.
    service, _client = make_e2b_service()
    handle = _ScriptedDaemonStartHandle(
        info_exit_codes=[0], pending_exit_code=1,
    )

    await service._start_docker_daemon(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
    )

    assert "launch" not in handle.calls
    assert "stop_engine" not in handle.calls
    assert handle.calls[-1] == "chmod"


@pytest.mark.asyncio
async def test_start_docker_daemon_adopts_pending_boot_daemon() -> None:
    # Not serving yet, but a boot-managed daemon is mid-startup (pending
    # probe exits 0). The method must ADOPT it — poll until stable — and
    # never launch a competing dockerd (which would unlink the systemd
    # socket path and die on the volume-store flock).
    service, _client = make_e2b_service()
    handle = _ScriptedDaemonStartHandle(
        info_exit_codes=[1, 0, 0, 0], pending_exit_code=0,
    )

    await service._start_docker_daemon(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
    )

    assert "launch" not in handle.calls
    assert "stop_engine" not in handle.calls
    assert handle.calls[-1] == "chmod"
    assert sum(1 for c in handle.calls if c == "info") == 4


@pytest.mark.asyncio
async def test_start_docker_daemon_stops_engine_before_manual_launch() -> None:
    # No daemon serving and none pending: the systemd engine must be
    # stopped (freeing the flock + socket path) BEFORE the manual launch.
    service, _client = make_e2b_service()
    handle = _ScriptedDaemonStartHandle(
        info_exit_codes=[1, 0, 0, 0], pending_exit_code=1,
    )

    await service._start_docker_daemon(  # pyright: ignore[reportPrivateUsage]
        cast(Any, handle),
    )

    assert "stop_engine" in handle.calls
    assert "launch" in handle.calls
    assert handle.calls.index("stop_engine") < handle.calls.index("launch")
    assert handle.calls[-1] == "chmod"


# =====================================================================
# SANDBOX guardrail tier (SBX-*) — reattach / docker-daemon / fail-fast
# / map_exit / resolver robustness. Fully mocked, no real E2B.
# =====================================================================


# ---------- SBX-1: set_timeout re-applied on the wake path ----------
#
# The stdin_pump variant of this test is gone with the mid-session
# reattach it pinned: a woken sandbox now retires its process and
# rebuilds the session, so the pump never calls set_timeout. The
# invariant still matters and still has a home — ``_try_reconnect``
# below — because E2B resets the idle timeout to its 300s default on
# every auto_resume, which would silently defeat the cost knob.
# SBX-2/3/4 pinned the same removed branch (set_timeout failure after
# reattach, reattach-then-send-fails, old-handle release failure); the
# wake path's replacements are
# ``test_session_retires_process_after_stream_dies`` and
# ``test_session_wake_kill_failure_still_fails_transport``.


@pytest.mark.asyncio
async def test_sbx1_set_timeout_reapplied_in_try_reconnect_path() -> None:
    """SBX-1 sibling: the boot ``_try_reconnect`` path
    (service.py:951) must also re-apply ``set_timeout`` after a
    successful ``connect_sandbox`` — the reconnect implicitly sets the
    SDK 300s default on auto_resume."""
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-reconnect", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    # Warmup session writes a live ref; preserve so it survives for the
    # reconnect attempt below.
    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")
    timeouts_before = len(mock.set_timeouts)
    # Second session reconnects to the live ref → set_timeout re-applied.
    async with service.session(
        session_id="reconnect", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("reconnect")
    new_timeouts = mock.set_timeouts[timeouts_before:]
    assert any(t.timeout_seconds == 60 for t in new_timeouts), (
        f"_try_reconnect must re-apply set_timeout(60); "
        f"new set_timeouts={new_timeouts}"
    )


@pytest.mark.asyncio
async def test_try_reconnect_reuses_sandbox_but_respawns_process() -> None:
    """The reconnect path keeps the sandbox and replaces the process.

    This is the whole shape of the wake fix. Reusing the sandbox is
    what keeps a wake cheap: the expensive part of a cold start is
    downloading the MCP's package (7-22 s per server in production)
    and that cache lives on the sandbox filesystem. Reusing the
    PROCESS is what produced the bug, because a process resumed from a
    snapshot still believes it owns TCP connections that were severed
    while it slept.

    So: one ``connect_sandbox``, no ``connect_command``, a
    ``kill_command`` aimed at the recorded pid, and a fresh
    ``run_command`` carrying the upstream's argv and env.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-respawn", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]

    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")

    ref_after_warmup = await persistence.get(
        org_id="acme", upstream_id=upstream.id,
    )
    assert ref_after_warmup is not None
    warm_sandbox_id = ref_after_warmup.sandbox_id
    warm_pid = ref_after_warmup.pid
    assert warm_sandbox_id is not None and warm_pid is not None

    creates_before = len(mock.creates)
    commands_before = len(mock.commands)

    async with service.session(
        session_id="woken", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("woken")

    assert len(mock.creates) == creates_before, (
        "the warm sandbox must be reused, not rebuilt; that reuse is "
        f"where the cold-start saving lives. creates={mock.creates}"
    )
    assert not mock.connect_commands, (
        "a woken sandbox must never reattach to the frozen process; "
        f"connect_commands={mock.connect_commands}"
    )
    assert [k.pid for k in mock.kill_commands] == [warm_pid], (
        "the recorded pid must be killed exactly once, or every wake "
        f"leaves a dead MCP server resident. kills={mock.kill_commands}"
    )
    respawns = mock.commands[commands_before:]
    assert len(respawns) == 1, (
        f"expected exactly one replacement process; got {respawns}"
    )
    assert respawns[0].argv == ["npx"], (
        f"the replacement must run the upstream's command; got {respawns[0]}"
    )

    ref_after = await persistence.get(org_id="acme", upstream_id=upstream.id)
    assert ref_after is not None
    assert ref_after.sandbox_id == warm_sandbox_id, "same sandbox"
    assert ref_after.pid != warm_pid, (
        "the ref must record the NEW pid, or the next wake kills a pid "
        "that no longer exists and reattaches nothing"
    )


@pytest.mark.asyncio
async def test_reuse_is_refused_when_the_size_changed() -> None:
    """A cpu/ram edit must force a new sandbox, not reuse the old size.

    Sandbox size is baked into the E2B template at create time, and
    reconnecting attaches by ``sandbox_id`` — it cannot re-size. So a
    reuse after the operator raised memory would silently keep running
    at the OLD size.

    That is not merely slow, it is a lie on the dashboard:
    ``cpu_vcpus`` and ``memory_mb`` are part of the runtime hash, so
    the dirty-config banner flags the edit, and ``connect_shared``
    re-persists that hash on every reopen. Reuse would clear the
    banner while the MCP kept running out of the memory the operator
    thought they had given it.

    Introduced by the preserve-the-sandbox fix and caught in the
    fourth review pass: before it, the reopen fresh-created at the new
    size, so clearing the banner was honest.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-resize", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]

    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=SandboxResources(cpu_vcpus=1, memory_mb=1024, disk_gb=0),
        denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")

    assert mock.creates[-1].template == "mcpolis-node-cpu1-ram1024"
    creates_before = len(mock.creates)

    # The operator raises memory and the upstream is reopened.
    async with service.session(
        session_id="bigger", org_id="acme", upstream=upstream,
        resources=SandboxResources(cpu_vcpus=2, memory_mb=2048, disk_gb=0),
        denylist=(),
    ):
        service.mark_session_preserve_on_close("bigger")

    assert len(mock.creates) == creates_before + 1, (
        "a size change must fresh-create; reusing the old sandbox "
        "keeps the old size while the dashboard reports the new one"
    )
    assert mock.creates[-1].template == "mcpolis-node-cpu2-ram2048", (
        f"the new sandbox must use the requested size; got "
        f"{mock.creates[-1].template}"
    )


@pytest.mark.asyncio
async def test_reuse_still_happens_when_the_size_is_unchanged() -> None:
    """The size check must not throw away the saving it guards.

    Same size, same template: reuse, so the wake stays cheap.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-same-size", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    resources = SandboxResources(cpu_vcpus=1, memory_mb=1024, disk_gb=0)

    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=resources, denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")
    creates_before = len(mock.creates)

    async with service.session(
        session_id="again", org_id="acme", upstream=upstream,
        resources=resources, denylist=(),
    ):
        service.mark_session_preserve_on_close("again")

    assert len(mock.creates) == creates_before, (
        "an unchanged size must still reuse the warm sandbox"
    )


@pytest.mark.asyncio
async def test_try_reconnect_survives_a_refused_kill() -> None:
    """A refused kill costs one resident process, never the session.

    E2B can refuse the kill (API blip, pid already reaped). Treating
    that as fatal would turn a cosmetic leak into a failed upstream,
    so the respawn proceeds regardless.
    """
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-kill-refused", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]

    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")

    creates_before = len(mock.creates)
    mock.kill_command_error = E2BSDKError("SandboxError", "kill refused")

    async with service.session(
        session_id="woken", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ) as session:
        assert session.transport_failed is not None
        assert not session.transport_failed.is_set(), (
            "a refused kill must still yield a usable session"
        )
        service.mark_session_preserve_on_close("woken")

    assert len(mock.creates) == creates_before, (
        "a refused kill is not a reason to abandon the warm sandbox"
    )
    assert mock.kill_commands, "the kill must still have been attempted"


# ---------- SBX-5: _start_docker_daemon TimeoutError + log capture -------


class _NeverReadyDaemonHandle:
    """Full ``_start_docker_daemon`` stand-in where the daemon never
    stabilizes. ``docker info`` always fails, pending probe says "no
    boot daemon" (manual launch path), and ``cat /tmp/dockerd.log``
    streams a scripted error back through ``on_stdout`` so the test can
    assert the raised ``TimeoutError`` carries that log text."""

    def __init__(self, *, dockerd_log: str) -> None:
        self._dockerd_log = dockerd_log
        self.calls: list[str] = []

    def _classify(self, argv: list[str]) -> str:
        joined = " ".join(argv)
        if argv[:2] == ["docker", "info"]:
            return "info"
        if "pgrep -x dockerd" in joined:
            return "pending_probe"
        if "systemctl stop" in joined:
            return "stop_engine"
        if "nohup sudo dockerd" in joined:
            return "launch"
        if argv[:2] == ["sudo", "chmod"]:
            return "chmod"
        if "[ -S /var/run/docker.sock ]" in joined:
            return "chmod_early"
        if argv[:1] == ["cat"]:
            return "read_log"
        raise AssertionError(f"unexpected command: {argv!r}")

    async def run_command(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        on_stdout: Callable[[bytes], Awaitable[None] | None],
        on_stderr: Callable[[bytes], Awaitable[None] | None],
    ) -> _ScriptedDockerProc:
        label = self._classify(argv)
        self.calls.append(label)
        if label == "read_log":
            result = on_stdout(self._dockerd_log.encode("utf-8"))
            if asyncio.iscoroutine(result):
                await result
            return _ScriptedDockerProc(0)
        if label == "info":
            return _ScriptedDockerProc(1)  # never ready
        if label == "pending_probe":
            return _ScriptedDockerProc(1)  # no boot daemon → manual launch
        return _ScriptedDockerProc(0)


@pytest.mark.asyncio
async def test_sbx5_start_docker_daemon_raises_timeout_with_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SBX-5 (P1): when the daemon never stabilizes, the FULL
    ``_start_docker_daemon`` flow (not just the isolated poll helper)
    must read ``/tmp/dockerd.log`` and raise ``TimeoutError`` carrying
    that log text so the operator can diagnose. Pins service.py:1231-1257.
    The poll budget is shrunk so the test runs fast."""
    service, _client = make_e2b_service()
    # Shrink the poll budget so the never-ready path exits quickly.
    monkeypatch.setattr(
        E2BSandboxService._poll_docker_ready,  # type: ignore[reportPrivateUsage]
        "__kwdefaults__",
        {"max_polls": 2, "poll_interval": 0.0, "required_consecutive": 3},
    )
    handle = _NeverReadyDaemonHandle(
        dockerd_log="failed to start daemon: flock timeout on metadata.db",
    )
    with pytest.raises(TimeoutError) as exc:
        await service._start_docker_daemon(  # pyright: ignore[reportPrivateUsage]
            cast(Any, handle),
        )
    msg = str(exc.value)
    assert "did not become ready" in msg
    assert "flock timeout on metadata.db" in msg, (
        f"TimeoutError must carry the /tmp/dockerd.log text; got {msg!r}"
    )
    assert "read_log" in handle.calls, "the dockerd log must be read"


@pytest.mark.asyncio
async def test_sbx5_start_docker_daemon_empty_log_renders_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SBX-5 sibling: an empty dockerd log still raises TimeoutError
    with an ``(empty)`` placeholder rather than a blank tail."""
    service, _client = make_e2b_service()
    monkeypatch.setattr(
        E2BSandboxService._poll_docker_ready,  # type: ignore[reportPrivateUsage]
        "__kwdefaults__",
        {"max_polls": 2, "poll_interval": 0.0, "required_consecutive": 3},
    )
    handle = _NeverReadyDaemonHandle(dockerd_log="")
    with pytest.raises(TimeoutError) as exc:
        await service._start_docker_daemon(  # pyright: ignore[reportPrivateUsage]
            cast(Any, handle),
        )
    assert "(empty)" in str(exc.value)


# ---------- SBX-6: respawn slow-fail → fresh-create fallback -----


@pytest.mark.asyncio
async def test_sbx6_try_reconnect_slow_fail_kills_and_fresh_creates() -> None:
    """SBX-6 (P1): in ``_try_reconnect`` a respawn that raises the
    timeout-shaped ``E2BSDKError`` (envd wedged after auto_resume)
    must kill the stale sandbox and fall through to a fresh create.

    The reconnect path reuses the sandbox but never its MCP process,
    so the wedge now surfaces on ``run_command`` rather than on the
    retired ``connect_command`` reattach. The recovery contract is
    unchanged: a sandbox we cannot spawn into is not worth keeping."""
    persistence = InMemorySandboxPersistenceRepository()
    upstream = make_upstream_definition(id="ups-slow", command="npx")
    service, mock = make_e2b_service(persistence=persistence)
    service._reuse_sandboxes_on_restart = True  # type: ignore[reportPrivateUsage]
    async with service.session(
        session_id="warmup", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("warmup")
    ref = await persistence.get(org_id="acme", upstream_id="ups-slow")
    assert ref is not None and ref.sandbox_id is not None
    alive_sandbox_id = ref.sandbox_id

    creates_before = len(mock.creates)
    kills_before = len(mock.kills)
    original_connect = mock.connect_sandbox

    async def _connect_with_wedged_envd(snapshot_id: str):  # type: ignore[no-untyped-def]
        handle = await original_connect(snapshot_id)

        async def _timeout_run_command(
            *_args: Any, **_kwargs: Any,
        ) -> None:
            # Same timeout shape the SDK surfaces when envd is wedged
            # after an auto_resume and will not accept a new process.
            raise E2BSDKError(
                "TimeoutException",
                "spawn did not establish within 10s "
                "(envd port not open after resume)",
            )

        handle.run_command = _timeout_run_command  # type: ignore[method-assign,assignment]
        return handle

    mock.connect_sandbox = _connect_with_wedged_envd  # type: ignore[method-assign,assignment]

    async with service.session(
        session_id="slow-recovery", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ):
        service.mark_session_preserve_on_close("slow-recovery")

    # _kill_stale_sandbox fired (the reconnect handle was killed) and a
    # fresh sandbox was created.
    assert len(mock.kills) > kills_before, (
        f"the stale sandbox must be killed after the slow respawn "
        f"failure; kills={mock.kills}"
    )
    assert len(mock.creates) == creates_before + 1, (
        f"expected one fresh-create after the slow-fail; creates={mock.creates}"
    )
    ref_after = await persistence.get(org_id="acme", upstream_id="ups-slow")
    assert ref_after is not None and ref_after.sandbox_id is not None
    assert ref_after.sandbox_id != alive_sandbox_id


# ---------- SBX-7 [BUG?]: oversized / chatty non-JSON stdout (E2B) -------


@pytest.mark.asyncio
async def test_sbx7_e2b_no_newline_stream_buffer_is_bounded() -> None:
    """[BUG?] SBX-7 (P1): a stdout stream that NEVER emits a newline must
    not be retained in memory unbounded. ``on_stdout`` keeps the whole
    accumulated stream in ``stdout_buffer[0]`` (``split('\\n')`` leaves it
    as the leftover), so 16 MiB of newline-free output is held verbatim
    while errlog stays empty.

    Pinned via tracemalloc RETENTION, not errlog size: a trailing-newline
    variant would assert on the flushed line, which an errlog-only cap
    would satisfy while leaving the in-memory DoS in place (and falsely
    flipping this guardrail green). This asserts the contract the bug
    actually violates — bounded in-memory retention."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    errlog = StringIO()
    fed = 16 * 1024 * 1024
    chunk = b"x" * (1024 * 1024)  # 1 MiB, NO newline ever
    sane_retained = 4 * 1024 * 1024  # 4 MiB: 4x below the fed size
    async with service.session(
        session_id="sbx7", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(), errlog=errlog,
    ) as session:
        read_stream = session.read_stream

        async def drain() -> None:
            async with read_stream:
                async for item in read_stream:
                    del item

        drain_task = asyncio.create_task(drain())
        on_stdout = mock.last_on_stdout
        assert on_stdout is not None
        gc.collect()
        tracemalloc.start()
        base, _ = tracemalloc.get_traced_memory()
        for _ in range(fed // len(chunk)):
            result = on_stdout(chunk)
            if asyncio.iscoroutine(result):
                await result
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        drain_task.cancel()
    retained = current - base
    # No complete line ever arrived, so the flushed-line (errlog) path is
    # not what's under test; assert it stayed bounded too, then pin the
    # real contract: the in-memory leftover buffer must be bounded.
    assert len(errlog.getvalue()) <= sane_retained
    assert retained <= sane_retained, (
        f"no-newline stdout retained {retained} bytes in memory "
        f"(fed {fed}); the leftover buffer is unbounded"
    )


@pytest.mark.asyncio
async def test_sbx7_e2b_chatty_npm_warn_lines_route_to_errlog() -> None:
    """SBX-7 (P1) — chatty path: 10k newline-framed npm-warn lines are
    each non-JSON and must all route to errlog (none silently dropped).
    This branch is well-behaved (newline-framed → bounded per line), so
    it passes; it documents the no-bug half of the spec."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    errlog = StringIO()
    async with service.session(
        session_id="sbx7-chatty", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(), errlog=errlog,
    ) as session:
        read_stream = session.read_stream

        async def drain() -> None:
            async with read_stream:
                async for item in read_stream:
                    del item

        drain_task = asyncio.create_task(drain())
        on_stdout = mock.last_on_stdout
        assert on_stdout is not None
        n = 10_000
        payload = "".join(
            f"npm warn deprecated pkg-{i}@1.0.0\n" for i in range(n)
        ).encode("utf-8")
        result = on_stdout(payload)
        if asyncio.iscoroutine(result):
            await result
        try:
            await asyncio.wait_for(drain_task, timeout=2.0)
        except asyncio.TimeoutError:
            drain_task.cancel()
    captured = errlog.getvalue()
    assert captured.count("npm warn deprecated") == n, (
        f"all {n} chatty lines must route to errlog; "
        f"got {captured.count('npm warn deprecated')}"
    )


# ---------- SBX-8: bogus-command fail-fast through _session_cm -----------


@pytest.mark.asyncio
async def test_sbx8_bogus_command_exit_during_init_fast_fails() -> None:
    """SBX-8 (P1): when the MCP process exits during init (E2B mock
    ``simulate_exit(127)``: command-not-found), the
    ``init_with_exit_race`` against the session's ``exit_signal`` must
    raise ``SubprocessExitedDuringInit`` with code 127 + the stderr
    tail — within the fast budget, not the full 120s INIT_TIMEOUT.
    Drives the E2B session's exit_signal end-to-end."""
    from mcpolis.adapters.upstream_clients.stdio_adapter import (
        SubprocessExitedDuringInit,
        init_with_exit_race,
    )

    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-bogus", command="npx")
    async with service.session(
        session_id="sbx8", org_id="acme", upstream=upstream,
        resources=make_default_resources(), denylist=(),
    ) as session:
        live_handle = cast(
            MockE2BSandboxHandle,
            service._live_sandboxes["sbx8"],  # type: ignore[reportPrivateUsage]
        )
        process = live_handle.last_process
        assert process is not None
        # Emit a stderr tail, then exit 127 (bash command-not-found).
        cb = mock.last_on_stderr
        assert cb is not None
        result = cb(b"npx: command not found: bogus-mcp\n")
        if asyncio.iscoroutine(result):
            await result
        process.simulate_exit(127)

        fake_session = _NeverInitSession()
        started = time.monotonic()
        with pytest.raises(SubprocessExitedDuringInit) as exc:
            await init_with_exit_race(
                cast(Any, fake_session), session.exit_signal, timeout=5.0,
            )
        elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"fail-fast must beat INIT_TIMEOUT; took {elapsed:.2f}s"
    assert exc.value.exit_code == 127
    assert "command not found" in exc.value.stderr_tail


@pytest.mark.asyncio
async def test_sbx8_init_with_exit_race_exit_wins_over_pending_init() -> None:
    """SBX-8 (init_with_exit_race semantics): when exit fires while
    init is still pending, the race raises ``SubprocessExitedDuringInit``
    carrying the exit code/stderr — mirrors the ``init_with_exit_race``
    contract used by the connection task on a bogus E2B command."""
    from mcpolis.adapters.sandbox_services.exit_signal import ExitSignalImpl
    from mcpolis.adapters.upstream_clients.stdio_adapter import (
        SubprocessExitedDuringInit,
        init_with_exit_race,
    )

    signal = ExitSignalImpl()
    signal.append_stderr(b"bogus startup failure\n")

    async def fire_exit() -> None:
        await asyncio.sleep(0.02)
        signal.mark_exited(127)

    asyncio.create_task(fire_exit())
    with pytest.raises(SubprocessExitedDuringInit) as exc:
        await init_with_exit_race(
            cast(Any, _NeverInitSession()), signal, timeout=5.0,
        )
    assert exc.value.exit_code == 127
    assert "bogus startup failure" in exc.value.stderr_tail


class _NeverInitSession:
    """``ClientSession`` stub whose ``initialize()`` never resolves —
    forces the exit-signal arm of ``init_with_exit_race`` to win."""

    async def initialize(self) -> object:
        await asyncio.Event().wait()
        return None


# ---------- SBX-12: off-grid resources reach session() ----------


@pytest.mark.asyncio
async def test_sbx12_off_grid_resources_raise_at_template_name() -> None:
    """SBX-12 (P2): if validate_resources is bypassed and an off-grid
    (cpu, ram) reaches ``session()``, the template lookup must raise
    (KeyError / ResourcesUnsupported) rather than silently resolving to
    a surprising template. ``_open_sandbox`` → ``grid.template_name``
    raises KeyError for an unpublished pairing (service.py:1418)."""
    service, mock = make_e2b_service()
    upstream = make_upstream_definition(id="ups-x", command="npx")
    # 1 vCPU + 8192 MiB is NOT in the published grid.
    off_grid = SandboxResources(cpu_vcpus=1.0, memory_mb=8192, disk_gb=0)
    with pytest.raises((KeyError, ResourcesUnsupported)):
        async with service.session(
            session_id="sbx12", org_id="acme", upstream=upstream,
            resources=off_grid, denylist=(),
        ):
            pass
    # No sandbox was created with a mis-resolved template.
    assert mock.creates == []


# ---------- SBX-13: map_exit exit_code=0 with error_class set ----------


def test_sbx13_map_exit_zero_code_with_error_class_pins_branch() -> None:
    """SBX-13 (P2): ``map_exit`` with ``exit_code=0`` AND an
    uncategorized ``error_class`` must NOT fall into the SUBPROCESS_EXITED
    branch (that's gated on a non-zero code) — it pins the
    ``PROVIDER_ERROR`` fallback. service.py:1830 requires
    ``exit_code != 0``."""
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        exit_code=0, error_class="SomeUncategorizedSDKError",
        raw_message="boom at zero",
    )
    reason, detail = service.map_exit(info)
    assert reason is ExitReason.PROVIDER_ERROR, (
        f"exit_code=0 + uncategorized error_class must map to "
        f"PROVIDER_ERROR, not SUBPROCESS_EXITED; got {reason}"
    )
    assert detail == "boom at zero"


def test_sbx13_map_exit_zero_code_with_auth_error_class_still_auth() -> None:
    """SBX-13 sibling: a categorized error_class (AuthError) wins even
    with exit_code=0 — the error-class branches are checked before the
    exit-code branch."""
    service, _ = make_e2b_service()
    info = ProviderExitInfo(
        exit_code=0, error_class="E2BAuthError", raw_message="bad key",
    )
    reason, _ = service.map_exit(info)
    assert reason is ExitReason.AUTH_FAILED


# ---------- SBX-14: SandboxResolver returns the global provider ----------
# NOTE: SandboxResolver lives in domain/services/sandbox_resolver.py, which
# the config/plumbing agent may also touch — possible test overlap. Keeping
# only this one-line guard here per the brief.


@pytest.mark.asyncio
async def test_sbx14_resolver_returns_global_provider() -> None:
    """SBX-14 (P2): day-one ``SandboxResolver.resolve`` ignores org_id
    and returns the configured global provider verbatim."""
    from mcpolis.domain.services.sandbox_resolver import SandboxResolver

    resolver = SandboxResolver(global_provider="e2b")
    assert await resolver.resolve(org_id="any-org") == "e2b"
    assert await resolver.resolve(org_id="other") == "e2b"
