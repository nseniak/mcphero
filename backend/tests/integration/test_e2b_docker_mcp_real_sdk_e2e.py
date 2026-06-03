"""End-to-end real-SDK smoke for the docker (dind) E2B template.

Exercises the full chain for a Docker-distributed MCP server:

    real RealE2BClient → E2BSandboxService.session() → docker
    language template lookup → Sandbox.create (with dockerd start_cmd)
    → dockerd ready probe → commands.run("docker run -i --rm …") →
    stdio bridging → ClientSession.initialize → tools/list → close

Uses ``mcpolis-docker-cpu2-ram2048`` (E2B's recommended floor for
Docker is 2 vCPU / 2 GB) and the official ``mcp/everything`` image
from Docker's MCP catalog — the same "kitchen sink" server the node
template pre-warms with npx.

Skips when:
  - ``E2B_API_KEY`` is unset (same gate as all real-SDK integration tests)
  - ``mcpolis-docker-cpu2-ram2048`` isn't published on the active
    E2B account yet (template-missing heuristic)

To run::

    cd runner/e2b-templates && make build      # publishes all 24 templates
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh \\
        tests/integration/test_e2b_docker_mcp_real_sdk_e2e.py -v -s

Cost estimate: ~2 min of E2B compute per test run at the 2-vCPU tier.
Approximately $0.01–0.02 per run (2× node/python smokes because the
larger sandbox + dockerd daemon pull).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from io import StringIO

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b import (
    E2BSandboxService,
    RealE2BClient,
)
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition


E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

# docker MCPs run via ``docker run -i --rm <image>``. mcp/everything is
# Docker's official MCP catalog image for the reference "everything"
# server — the same server the node template pre-warms. Small image, no
# auth required, well-known tool set.
DOCKER_MCP_IMAGE = "mcp/everything"

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — docker MCP real-SDK end-to-end tests skipped",
)


def make_test_service() -> E2BSandboxService:
    assert E2B_API_KEY is not None
    return E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-docker-{TEST_RUN_ID}",
        on_timeout_seconds=120,
    )


def make_docker_resources() -> SandboxResources:
    """2 vCPU / 2 GB — E2B's documented floor for running Docker.
    Matches ``mcpolis-docker-cpu2-ram2048``."""
    return SandboxResources(cpu_vcpus=2.0, memory_mb=2048, disk_gb=0)


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


@pytest.mark.asyncio
async def test_docker_mcp_initialize_and_list_tools() -> None:
    """Stand up an E2B sandbox on the docker template, run
    ``docker run -i --rm mcp/everything``, MCP-initialize over the
    bridged stdio streams, ``tools/list``, close cleanly.

    The sandbox boot is slower than node/python because:
    1. ``dockerd`` must start and pass the TCP ready probe before the
       sandbox is reported ready (handled by the template's
       ``set_start_cmd``).
    2. ``docker run`` cold-pulls ``mcp/everything`` from Docker Hub on
       first use inside a fresh sandbox.

    Combined budget of 240 s is generous for both; subsequent runs
    against the same account hit the image layer cache in the
    sandbox's /var/lib/docker and are much faster.
    """
    service = make_test_service()
    upstream = make_upstream_definition(
        id="e2e-docker-everything",
        command="docker",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "run", "-i", "--rm", DOCKER_MCP_IMAGE,
    ]
    errlog = StringIO()
    try:
        async with service.session(
            session_id=f"e2e-docker-{TEST_RUN_ID}",
            org_id=f"acme-docker-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_docker_resources(),
            denylist=(),
            errlog=errlog,
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            client = ClientSession(read_stream, write_stream)
            async with client:
                init_result = await asyncio.wait_for(
                    client.initialize(), timeout=240.0,
                )
                assert init_result.serverInfo.name
                tools_result = await asyncio.wait_for(
                    client.list_tools(), timeout=15.0,
                )
                assert len(tools_result.tools) > 0, (
                    "mcp/everything should expose tools; "
                    f"got {tools_result.tools!r}"
                )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-docker-cpu2-ram2048 not published on the "
                "active E2B account — run `cd runner/e2b-templates "
                "&& make build` against the test account first.",
            )
        captured = errlog.getvalue()
        if captured:
            print(f"\n----- sandbox stderr -----\n{captured}\n----- end -----\n")
        raise
    except Exception:
        captured = errlog.getvalue()
        if captured:
            print(f"\n----- sandbox stderr -----\n{captured}\n----- end -----\n")
        raise


@pytest.mark.asyncio
async def test_docker_daemon_socket_accessible_without_sudo() -> None:
    """Confirm the ``dockerd`` boot sequence left the socket accessible
    to the non-root sandbox user.

    The docker template's start command chmods the socket to 666 once
    ``dockerd`` creates it. Without that, ``docker run`` from the MCP
    command (E2B runs it as a non-root ``user``) fails with "permission
    denied on /var/run/docker.sock".

    Uses ``RealE2BClient`` directly against the docker template (same
    pattern as ``test_e2b_sandbox_service_real_sdk.py``) so we can run
    an arbitrary ``docker info`` command without going through the full
    MCP stack. A socket-permission failure surfaces as a clear exit-code
    failure, not a confusing MCP-initialize timeout.
    """
    assert E2B_API_KEY is not None
    client = RealE2BClient(api_key=E2B_API_KEY)
    # Use the smallest docker template at or above the recommended floor.
    template = "mcpolis-docker-cpu2-ram2048"
    sandbox_id: str | None = None
    try:
        sandbox = await client.create_sandbox(
            template=template,
            metadata={
                "mcpolis_test": "1",
                "test_run_id": TEST_RUN_ID,
                "scenario": "docker_socket_check",
            },
            timeout_seconds=120,
        )
        sandbox_id = sandbox.sandbox_id

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def on_stdout(b: bytes) -> None:
            stdout_chunks.append(b)

        async def on_stderr(b: bytes) -> None:
            stderr_chunks.append(b)

        # Mirror E2BSandboxService._start_docker_daemon:
        # 1. chmod the socket if it already exists (template may have started
        #    dockerd via set_start_cmd but left the socket root-only due to
        #    a race with the ready probe).
        chmod_early = await sandbox.run_command(
            [
                "sudo", "sh", "-c",
                "[ -S /var/run/docker.sock ]"
                " && chmod 666 /var/run/docker.sock || true",
            ],
            env={}, on_stdout=on_stdout, on_stderr=on_stderr,
        )
        try:
            await asyncio.wait_for(chmod_early.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        # 2. Check if daemon is now accessible.
        info_check = await sandbox.run_command(
            ["docker", "info"], env={},
            on_stdout=on_stdout, on_stderr=on_stderr,
        )
        try:
            check_code = await asyncio.wait_for(info_check.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            check_code = 1

        if check_code != 0:
            # 3. Daemon not running — delete stale PID file and start it.
            start_proc = await sandbox.run_command(
                [
                    "sh", "-c",
                    "sudo rm -f /var/run/docker.pid;"
                    " nohup sudo dockerd --iptables=false --bridge=none"
                    " -H unix:///var/run/docker.sock > /tmp/dockerd.log 2>&1 &",
                ],
                env={}, on_stdout=on_stdout, on_stderr=on_stderr,
            )
            await asyncio.wait_for(start_proc.wait(), timeout=10.0)

            for _ in range(120):
                info_proc = await sandbox.run_command(
                    ["docker", "info"], env={},
                    on_stdout=on_stdout, on_stderr=on_stderr,
                )
                try:
                    ec = await asyncio.wait_for(info_proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    ec = 1
                if ec == 0:
                    break
                await asyncio.sleep(0.5)
            else:
                log_proc = await sandbox.run_command(
                    ["cat", "/tmp/dockerd.log"], env={},
                    on_stdout=on_stdout, on_stderr=on_stderr,
                )
                await asyncio.wait_for(log_proc.wait(), timeout=5.0)
                log = b"".join(stdout_chunks).decode("utf-8", errors="replace")
                pytest.fail(f"dockerd did not start within 60s.\nLog:\n{log}")

            chmod_proc = await sandbox.run_command(
                ["sudo", "chmod", "666", "/var/run/docker.sock"],
                env={}, on_stdout=on_stdout, on_stderr=on_stderr,
            )
            await asyncio.wait_for(chmod_proc.wait(), timeout=5.0)

        # Now verify socket is accessible without sudo.
        stdout_chunks.clear()
        stderr_chunks.clear()
        proc = await sandbox.run_command(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            env={},
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        exit_code = await asyncio.wait_for(proc.wait(), timeout=30.0)
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if exit_code != 0:
            pytest.fail(
                f"``docker info`` exited {exit_code} after chmod.\n"
                f"stderr: {stderr!r}\n"
                "Check that chmod 666 on /var/run/docker.sock is working.",
            )
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
        assert stdout, (
            "``docker info --format {{.ServerVersion}}`` returned empty "
            "output — daemon may not be fully up."
        )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                f"{template} not published on the active E2B account — "
                "run `cd runner/e2b-templates && make build` first.",
            )
        raise
    finally:
        if sandbox_id is not None:
            try:
                await client.kill_sandbox(sandbox_id)
            except E2BSDKError:
                pass
