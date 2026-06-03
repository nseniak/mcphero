"""Contract: ``session()`` opens, yields streams, closes cleanly.

The MCP-level round trip (``initialize`` + ``tools/list``) is covered
in the per-backend integration suites; this module locks down the
context-manager protocol itself: open → yield ``(read, write)`` →
close, with state-registry transitions tracked alongside.
"""
from __future__ import annotations

import pytest

from mcpolis.domain.services.sandbox_service import (
    SandboxResources,
    SandboxService,
    SandboxSession,
)

from tests.unit.sandbox_service_contract.backends import iter_session_backends


def _default_resources(service: SandboxService) -> SandboxResources:
    caps = service.capabilities()
    return SandboxResources(
        cpu_vcpus=caps.allowed_cpu_vcpus[0],
        memory_mb=caps.allowed_memory_mb[0],
        disk_gb=caps.allowed_disk_gb[0] if caps.allowed_disk_gb else 0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service", iter_session_backends())
async def test_session_yields_streams_and_closes_cleanly(
    service: SandboxService,
) -> None:
    """Round-trip the context manager itself: open, observe the two
    streams + per-session exit signal, close. End-to-end MCP traffic
    is exercised in the per-backend test modules."""
    from tests.unit.factories import make_upstream_definition

    # ``cat`` is a portable no-op subprocess: reads stdin, echoes,
    # exits cleanly on EOF. Avoids the parser crash we'd hit with
    # ``echo`` (which emits an empty line before exiting and trips
    # stdio_client's JSON-RPC validator).
    upstream = make_upstream_definition(id="contract-mcp", command="cat")
    resources = _default_resources(service)
    async with service.session(
        session_id="contract-session",
        org_id="contract-org",
        upstream=upstream,
        resources=resources,
        denylist=(),
    ) as streams:
        assert streams.read_stream is not None
        assert streams.write_stream is not None
        # Every backend must wire up the exit signal so the
        # connection task's fail-fast race has something to await.
        assert streams.exit_signal is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("service", iter_session_backends())
async def test_session_streams_typed_correctly(
    service: SandboxService,
) -> None:
    """The yielded pair must match the
    ``mcp.client.stdio.stdio_client`` shape so a wrapping
    ``ClientSession`` works unchanged across backends."""
    from tests.unit.factories import make_upstream_definition

    upstream = make_upstream_definition(id="contract-mcp", command="cat")
    resources = _default_resources(service)
    async with service.session(
        session_id="contract-session",
        org_id="contract-org",
        upstream=upstream,
        resources=resources,
        denylist=(),
    ) as streams:
        read, write = streams.read_stream, streams.write_stream
        assert hasattr(read, "receive")
        assert hasattr(write, "send")
        _: SandboxSession = streams
