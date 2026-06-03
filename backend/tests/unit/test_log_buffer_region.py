"""``LogBufferRegion`` contract tests.

The region was carved out of ``UpstreamClientManager`` as the
warm-up for the manager-region split (internal/plans/manager-region-split.md,
Phase 1). Tests target the region directly — no manager
construction, no sandbox plumbing. This demonstrates the
"independent testability" win the split exists to deliver: the
two methods drive a flat dict; the test surface is correspondingly
flat.
"""
from __future__ import annotations

import asyncio

import pytest

from mcpolis.adapters.upstream_clients.log_buffer_region import LogBufferRegion


@pytest.mark.asyncio
async def test_get_returns_none_when_no_buffer_exists() -> None:
    region = LogBufferRegion()
    assert region.get("never-opened") is None
    assert region.get_output("never-opened") is None


@pytest.mark.asyncio
async def test_get_or_create_returns_same_buffer_for_same_id() -> None:
    """Idempotency: log capture must accumulate across reconnects,
    so the second call for the same upstream returns the exact
    object the first call created — not a fresh empty buffer."""
    region = LogBufferRegion()
    first = region.get_or_create("notion")
    second = region.get_or_create("notion")
    assert first is second


@pytest.mark.asyncio
async def test_get_or_create_returns_distinct_buffers_for_distinct_ids() -> None:
    region = LogBufferRegion()
    notion = region.get_or_create("notion")
    slack = region.get_or_create("slack")
    assert notion is not slack


@pytest.mark.asyncio
async def test_get_output_reads_through_to_buffer_contents() -> None:
    """``get_output`` is the user-visible accessor backing the
    log-viewer UI. It must reflect whatever the underlying buffer
    has captured."""
    region = LogBufferRegion()
    buf = region.get_or_create("notion")
    buf.write("startup line")

    output = region.get_output("notion")
    assert output is not None
    assert "startup line" in output


@pytest.mark.asyncio
async def test_buffer_persists_across_repeated_lookups() -> None:
    """The lifecycle contract: buffers outlive session reconnects.
    Once created, the same buffer survives unbounded ``get`` /
    ``get_or_create`` calls without being replaced."""
    region = LogBufferRegion()
    buf = region.get_or_create("notion")
    buf.write("first chunk")

    # Simulate session bounce: more lookups, a write between, more
    # lookups. The buffer object never changes, contents accumulate.
    assert region.get("notion") is buf
    buf.write("second chunk")
    assert region.get_or_create("notion") is buf

    output = region.get_output("notion")
    assert output is not None
    assert "first chunk" in output
    assert "second chunk" in output


@pytest.mark.asyncio
async def test_get_or_create_binds_buffer_to_running_loop() -> None:
    """The buffer needs an event-loop binding for thread-safe
    ``write()`` from the stdio adapter's reader thread. The region
    binds it on creation; if the binding were skipped, the first
    cross-thread write would crash."""
    region = LogBufferRegion()
    buf = region.get_or_create("notion")
    # Binding is internal state — sanity-check by exercising the
    # cross-thread write path the binding exists to support.
    await asyncio.to_thread(buf.write, "from worker thread")
    output = region.get_output("notion")
    assert output is not None
    assert "from worker thread" in output
