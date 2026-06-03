"""Real-SDK proof that ``StdioInitTimeout`` surfaces with the
expected message when an stdio MCP hangs at the JSON-RPC initialize
handshake.

Companion to the unit-level test in
``test_subprocess_exit_fail_fast.py`` — that test drives the race
helper with a stub session; this test stands up a real E2B sandbox
and runs ``python3 -c "import sys; sys.stdin.read()"`` (which hangs
waiting on stdin without ever responding to ``initialize``). With a
reduced ``init_with_exit_race`` timeout (5 s, not the production
120 s), the race fires the new exception within the test's
wall-clock budget.

Skips when ``E2B_API_KEY`` is unset.
"""
from __future__ import annotations

import os
import uuid

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b import (
    E2BSandboxService,
    RealE2BClient,
)
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    StdioInitTimeout,
    init_with_exit_race,
)
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK init-timeout test skipped",
)


def _make_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def _is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


@pytest.mark.asyncio
async def test_stdio_init_timeout_surfaces_against_hanging_mcp() -> None:
    """A stdio process that reads stdin without responding fires the
    new :class:`StdioInitTimeout` after the configured timeout.

    Uses a 5 s timeout (vs production 120 s) so the test runs in
    well under a minute. The exact wall-clock is dominated by E2B
    cold-create + npm/uv pre-warm; the race itself is bounded by
    our reduced timeout."""
    assert E2B_API_KEY is not None
    upstream = make_upstream_definition(
        id=f"e2e-hang-{TEST_RUN_ID}", command="python3",
    )
    # ``sys.stdin.read()`` blocks forever waiting on EOF; the MCP
    # client never gets a response to ``initialize``. The race
    # helper's exit-task can't fire (the process is still alive),
    # so the timeout branch wins.
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-c", "import sys; sys.stdin.read()",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-hang-{TEST_RUN_ID}",
        on_timeout_seconds=60,
    )
    try:
        async with service.session(
            session_id=f"e2e-hang-{TEST_RUN_ID}",
            org_id=f"acme-{TEST_RUN_ID}",
            upstream=upstream,
            resources=_make_resources(),
            denylist=(),
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream,
                sandbox_session.write_stream,
            )
            async with session:
                with pytest.raises(StdioInitTimeout) as exc_info:
                    await init_with_exit_race(
                        session, sandbox_session.exit_signal,
                        timeout=5.0,
                    )
                msg = str(exc_info.value)
                assert "did not initialise" in msg
                assert "5s" in msg
                assert "browser" in msg
    except E2BSDKError as exc:
        if _is_template_missing_error(exc):
            pytest.skip(
                "mcpolis-python-cpu1-ram1024 not published on the "
                "active E2B account — run `cd runner/e2b-templates "
                "&& make build` first.",
            )
        raise
