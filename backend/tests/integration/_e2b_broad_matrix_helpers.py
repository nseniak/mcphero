"""Shared helpers for the split E2B broad-matrix integration suite.

This is NOT a test module (underscore prefix → pytest never collects
it). It holds the constants and ``make_XXX`` builders that the
``test_e2b_m_*_e2e.py`` sibling files used to share at module level
inside the original single ``test_e2b_broad_matrix_e2e.py`` file.

The broad-matrix tests now run in the standard paid integration leg
(``run-integration-tests.sh``, ``make test-all``) whenever
``E2B_API_KEY`` is present and ``NO_INTEGRATION`` is unset — they are
split across several files purely so ``--dist loadfile`` spreads them
over the xdist workers instead of running the whole sweep serially on
one worker. Each split file carries its own API-key ``skipif`` marker;
this module only provides the shared building blocks.
"""
from __future__ import annotations

import os
import uuid

from mcpolis.adapters.sandbox_e2b import E2BSandboxService, RealE2BClient
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

IDLE_PAUSE_SECONDS = 30
REATTACH_WAIT_SECONDS = IDLE_PAUSE_SECONDS + 5
INITIALIZE_TIMEOUT = 120.0
DOCKER_INITIALIZE_TIMEOUT = 240.0
TOOL_CALL_TIMEOUT = 30.0

# Docker MCP image from Docker's official MCP catalog (mirrors the
# node template's npx server-everything pre-warm).
DOCKER_MCP_IMAGE = "mcp/everything"


def make_test_metadata(scenario: str) -> dict[str, str]:
    return {
        "mcpolis_test": "1",
        "test_run_id": TEST_RUN_ID,
        "scenario": scenario,
    }


def make_test_client() -> RealE2BClient:
    assert E2B_API_KEY is not None  # guarded by pytestmark
    return RealE2BClient(api_key=E2B_API_KEY)


def make_service(
    *,
    instance: str,
    on_timeout_seconds: int = IDLE_PAUSE_SECONDS,
) -> E2BSandboxService:
    assert E2B_API_KEY is not None  # guarded by pytestmark
    return E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=instance,
        on_timeout_seconds=on_timeout_seconds,
    )


def make_resources(cpu_vcpus: float, memory_mb: int) -> SandboxResources:
    return SandboxResources(
        cpu_vcpus=cpu_vcpus, memory_mb=memory_mb, disk_gb=0,
    )


def make_node_upstream(suffix: str) -> UpstreamDefinition:
    upstream = make_upstream_definition(
        id=f"e2e-m-{suffix}-{TEST_RUN_ID}", command="npx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "-y", "@modelcontextprotocol/server-everything",
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]
    return upstream


def make_uvx_time_upstream(suffix: str) -> UpstreamDefinition:
    """A real uvx Python MCP — ``mcp-server-time`` exposes
    ``get_current_time`` (requires a ``timezone`` arg)."""
    upstream = make_upstream_definition(
        id=f"e2e-m-{suffix}-{TEST_RUN_ID}", command="uvx",
    )
    upstream.stdio.args = ["mcp-server-time"]  # type: ignore[union-attr]
    upstream.stdio.env = {}  # type: ignore[union-attr]
    return upstream


def make_docker_upstream(suffix: str) -> UpstreamDefinition:
    upstream = make_upstream_definition(
        id=f"e2e-m-{suffix}-{TEST_RUN_ID}", command="docker",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "run", "-i", "--rm", DOCKER_MCP_IMAGE,
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]
    return upstream


def is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


async def sweep_kill(client: RealE2BClient, instance: str) -> None:
    """Best-effort: kill every sandbox tagged to ``instance``."""
    try:
        infos = await client.list_sandboxes(
            metadata_filter={"mcpolis_instance": instance},
        )
        for info in infos:
            try:
                await client.kill_sandbox(info.sandbox_id)
            except E2BSDKError:
                pass
    except E2BSDKError:
        pass
