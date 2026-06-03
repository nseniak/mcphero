"""End-to-end real-SDK smoke for ``E2BSandboxService``.

Exercises the full chain that a production MCP session goes through:

    real RealE2BClient → E2BSandboxService.session() → template
    lookup against the published grid → Sandbox.create → commands.run
    → stdio bridging → ClientSession.initialize → tools/list →
    pause → resume → kill

Distinct from ``test_e2b_sandbox_service_real_sdk.py`` (which targets
``RealE2BClient`` directly against ``template="base"``). This module
needs the mcpolis-* templates to be published on the active E2B
account; tests skip with a clear message when either the
``E2B_API_KEY`` env var is unset OR the first ``Sandbox.create``
fails with "template not found".

To run::

    cd runner/e2b-templates && make build      # one-time, ~15 min
    export E2B_API_KEY=...
    bash backend/run-integration-tests.sh tests/integration/test_e2b_service_real_sdk_e2e.py -v -s

Each test allocates ~30 s of E2B compute. Two scenarios = ~60 s,
~$0.005 of vCPU·s.
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


pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK end-to-end tests skipped",
)


def make_test_service() -> E2BSandboxService:
    """Build the service against the live API key + a unique
    instance id per run so reconciler-level isolation between
    parallel CI jobs holds."""
    assert E2B_API_KEY is not None
    return E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-{TEST_RUN_ID}",
        on_timeout_seconds=60,
    )


def make_default_resources() -> SandboxResources:
    """Smallest published combo to keep per-test cost minimal.
    Matches mcpolis-node-cpu1-ram1024 / mcpolis-python-cpu1-ram1024."""
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def is_template_missing_error(exc: BaseException) -> bool:
    """Heuristic: does this E2B SDK error indicate the requested
    template hasn't been built? When the operator hasn't run
    ``make e2b-templates-build`` against the active account, every
    test in this module hits the same error class. Skip rather
    than fail so the suite stays useful in CI accounts where the
    grid isn't pre-built."""
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return (
        "template" in needle
        and ("not found" in needle or "404" in needle)
    )


@pytest.mark.asyncio
async def test_real_e2e_initialize_and_list_tools() -> None:
    """The headline integration test: stand up a real E2B sandbox
    against the published mcpolis-node-cpu1-ram1024 template, run
    npx server-everything, MCP-initialize over the bridged stdio
    streams, ``tools/list``, close cleanly."""
    service = make_test_service()
    upstream = make_upstream_definition(
        id="e2e-server-everything", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    errlog = StringIO()
    try:
        async with service.session(
            session_id=f"e2e-{TEST_RUN_ID}-tools",
            org_id=f"acme-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
            errlog=errlog,
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            session = ClientSession(read_stream, write_stream)
            async with session:
                # The MCP cold-pull + start adds a few seconds on
                # top of E2B sandbox boot. ``server-everything`` is
                # globally pre-installed in the template
                # (Dockerfile.node line ~30), so npx -y should find
                # it cached and start fast — but a stuck binary or
                # network hiccup can blow past the budget. 120s is
                # generous; if it still times out the errlog dump
                # below tells us why.
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=120.0,
                )
                assert init_result.serverInfo.name
                tools_result = await asyncio.wait_for(
                    session.list_tools(), timeout=15.0,
                )
                assert len(tools_result.tools) > 0
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-node-cpu1-ram1024 not published on the "
                "active E2B account — run `cd runner/e2b-templates "
                "&& make build` against the test account first.",
            )
        # Surface stderr from the sandboxed npx process so we can
        # see *why* it failed (e.g. uvx/npx error, network blip,
        # MCP crash). Without this dump, debugging requires
        # rerunning with -s and patching in print()s.
        captured = errlog.getvalue()
        if captured:
            print(f"\n----- sandbox stderr -----\n{captured}\n----- end -----\n")
        raise
    except Exception:
        # Same dump for non-SDK failures (timeouts, JSON-RPC parse
        # errors, anyio stream issues). The test's own assertions
        # are also reached via this path on AssertionError.
        captured = errlog.getvalue()
        if captured:
            print(f"\n----- sandbox stderr -----\n{captured}\n----- end -----\n")
        raise


@pytest.mark.asyncio
async def test_real_e2e_pause_resume_round_trip() -> None:
    """Open a session, pause it via service.pause(), then open a
    new session passing ``resume_from`` — the SDK reconnects to
    the snapshot rather than create()-ing a fresh sandbox.
    Validates the full pause/resume primitive end-to-end against
    the live SDK."""
    service = make_test_service()
    upstream = make_upstream_definition(
        id="e2e-pause-resume", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    session_id_one = f"e2e-{TEST_RUN_ID}-resume-1"
    snapshot_ref = None
    try:
        async with service.session(
            session_id=session_id_one,
            org_id=f"acme-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
        ):
            # Pause while the session is live.
            snapshot_ref = await service.pause(session_id_one)
            assert snapshot_ref is not None
            assert snapshot_ref.provider == "e2b"
            assert snapshot_ref.snapshot_id

        # Reopen, asking the service to resume from the snapshot.
        # The SDK's connect-by-id should succeed without a fresh
        # create. We only need to verify the session opens — no
        # MCP traffic required for the resume-path assertion.
        async with service.session(
            session_id=f"e2e-{TEST_RUN_ID}-resume-2",
            org_id=f"acme-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_default_resources(),
            denylist=(),
            resume_from=snapshot_ref,
        ) as session:
            read_stream, write_stream = session.read_stream, session.write_stream
            assert read_stream is not None
            assert write_stream is not None
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-node-cpu1-ram1024 not published — run the "
                "template grid build first.",
            )
        raise
