"""Broad-matrix real-SDK guardrail: every CPU/RAM tier boots (E2B-M2).

Part of the split E2B broad-matrix suite. These now run in the
standard paid integration leg (``run-integration-tests.sh``,
``make test-all``) whenever ``E2B_API_KEY`` is set and
``NO_INTEGRATION`` is unset — the suite was split across
``test_e2b_m_*_e2e.py`` siblings so ``--dist loadfile`` spreads the
cost across the xdist workers instead of running the whole sweep
serially on one worker.

This file holds the heaviest single test — the 8-tier parametrized
``every_tier_boots`` sweep — alone, so it is its own long-pole rather
than dragging a sibling. Cost: one create + MCP-initialize per
published node tier (8 sandboxes), order ~$0.05-0.10 across the file.
Every sandbox is tagged with a per-run UUID and torn down by
``service.session()`` so a parallel run never sees another run's
sandboxes.
"""
from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.sandbox_e2b.template_grid import (
    CPU_RAM_PAIRS,
    template_name_for,
)
from tests.integration._e2b_broad_matrix_helpers import (
    E2B_API_KEY,
    INITIALIZE_TIMEOUT,
    TEST_RUN_ID,
    is_template_missing_error,
    make_node_upstream,
    make_resources,
    make_service,
)

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="needs a live E2B account (E2B_API_KEY unset)",
)


# ---------------------------------------------------------------------------
# E2B-M2 — every CPU/RAM tier boots (node), parametrized over the grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("cpu_vcpus", "memory_mb"), CPU_RAM_PAIRS)
@pytest.mark.asyncio
async def test_e2b_m2_every_tier_boots_broad_matrix(
    cpu_vcpus: int, memory_mb: int,
) -> None:
    """E2B-M2: one create + MCP-initialize per published CPU/RAM tier
    on the node grid. Imports ``CPU_RAM_PAIRS`` from the prod template
    grid module (never hardcoded) so adding a tier automatically
    extends the sweep. Proves every published node template actually
    boots + serves an MCP handshake."""
    template = template_name_for(
        language="node", cpu_vcpus=float(cpu_vcpus), memory_mb=memory_mb,
    )
    instance = f"e2e-m2-{cpu_vcpus}-{memory_mb}-{TEST_RUN_ID}"
    service = make_service(instance=instance)
    upstream = make_node_upstream(f"m2-{cpu_vcpus}-{memory_mb}")
    errlog = StringIO()
    try:
        async with service.session(
            session_id=instance,
            org_id=f"acme-m2-{TEST_RUN_ID}",
            upstream=upstream,
            resources=make_resources(float(cpu_vcpus), memory_mb),
            denylist=(),
            errlog=errlog,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream, sandbox_session.write_stream,
            )
            async with session:
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=INITIALIZE_TIMEOUT,
                )
                assert init_result.serverInfo.name, (
                    f"{template} booted but serverInfo.name was empty"
                )
    except E2BSDKError as exc:
        if is_template_missing_error(exc):
            pytest.skip(
                f"{template} not published on the active E2B account — "
                "run `cd runner/e2b-templates && make build` first.",
            )
        tail = errlog.getvalue()
        if tail:
            print(f"\n----- sandbox stderr -----\n{tail}\n----- end -----\n")
        raise
