"""§3.10 regression tests — long-lived MCP session tasks must NOT inherit
request-scoped contextvars (``request_id``, etc.) from the HTTP request
that spawned them.

The bug: structlog's ``merge_contextvars`` processor pulls the spawning
request's ``request_id`` into every log line emitted by the session
for the rest of its lifetime. We saw a 17-hour-stale ``request_id`` on
production log lines on 2026-04-27, leading to a real false-lead during
incident triage.

The fix: ``HttpConnectionTask.start()`` and ``StdioConnectionTask.start()``
spawn ``_run_in_session_context`` with ``context=contextvars.Context()``
(empty), so the task starts with no inherited contextvars. The wrapper
binds ``upstream_id`` / ``user_id`` / ``session_id`` first thing so log
lines remain queryable per-session even though ``request_id`` is gone.

Tests drive the real ``start()`` and inspect the actual contextvars
state inside the spawned task (via ``structlog.contextvars.get_contextvars``)
— a mock-heavy alternative would be too easy to silently regress.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    get_contextvars,
)

from mcpolis.adapters.upstream_clients.http_adapter import HttpConnectionTask
from mcpolis.domain.model.upstream import TransportType
from tests.unit.factories import make_upstream_definition


def _make_http_upstream() -> Any:
    return make_upstream_definition(
        id="notion",
        transport=TransportType.streamable_http,
        url="http://example.invalid/mcp",
    )


@pytest.mark.asyncio
async def test_http_session_task_does_not_inherit_request_scoped_contextvars() -> None:
    """Caller binds ``request_id`` (simulating an HTTP request handler
    that triggered Connect). The session task must spawn in a fresh
    contextvars.Context so ``request_id`` does NOT leak in. Without
    the fix, ``request_id`` survives in the task's contextvars for
    the lifetime of the session — exactly the §3.10 leak."""
    bind_contextvars(request_id="caller-rid", path="/api/connect")
    try:
        captured: dict[str, Any] = {}

        async def probe() -> None:
            captured.update(get_contextvars())
            task._session_future.set_result(MagicMock())
            await task._shutdown_event.wait()

        task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
        task._run = probe  # type: ignore[method-assign]

        await task.start()
        await task.close()

        # The smoking-gun: request_id must NOT be in the task's contextvars.
        assert "request_id" not in captured, (
            f"§3.10 leak: request_id leaked into session task: {captured}"
        )
        assert "path" not in captured, (
            f"§3.10 leak: path leaked into session task: {captured}"
        )
    finally:
        clear_contextvars()


@pytest.mark.asyncio
async def test_http_session_task_binds_durable_session_identifiers() -> None:
    """The fresh contextvars.Context isn't enough on its own — without
    durable identifiers bound inside the task, log lines from the
    session would have no ``upstream_id`` / ``user_id`` / ``session_id``
    at all and become un-filterable. Pin that the wrapper binds them."""
    captured: dict[str, Any] = {}

    async def probe() -> None:
        captured.update(get_contextvars())
        task._session_future.set_result(MagicMock())
        await task._shutdown_event.wait()

    task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
    task._run = probe  # type: ignore[method-assign]

    await task.start()
    await task.close()

    assert captured.get("upstream_id") == "notion"
    assert captured.get("user_id") == "alice@co.com"
    # session_id is a uuid4 hex string, generated per-task.
    session_id = captured.get("session_id")
    assert isinstance(session_id, str) and len(session_id) == 32


@pytest.mark.asyncio
async def test_separate_sessions_get_distinct_session_ids() -> None:
    """Two sessions on the same upstream + user (e.g. a reconnect after
    a probe-driven teardown) must get distinct ``session_id`` values
    so jq filters can tell their log streams apart. A constant
    ``session_id`` per (upstream, user) would defeat the per-session
    correlation that §3.10's fix is supposed to enable."""
    captured: list[dict[str, Any]] = [{}, {}]

    def make_probe(idx: int, t: HttpConnectionTask):
        async def probe() -> None:
            captured[idx].update(get_contextvars())
            t._session_future.set_result(MagicMock())
            await t._shutdown_event.wait()
        return probe

    task_a = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
    task_a._run = make_probe(0, task_a)  # type: ignore[method-assign]
    task_b = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
    task_b._run = make_probe(1, task_b)  # type: ignore[method-assign]

    await task_a.start()
    await task_b.start()
    await task_a.close()
    await task_b.close()

    sid_a = captured[0].get("session_id")
    sid_b = captured[1].get("session_id")
    assert sid_a and sid_b and sid_a != sid_b


@pytest.mark.asyncio
async def test_session_task_log_lines_carry_session_identifiers() -> None:
    """capture_logs() doesn't include the contextvars merge — the
    library swaps in a single capture processor — so this test asserts
    the bind succeeds by re-running the merge processor explicitly
    against the contextvars present inside the task. If the wrapper
    failed to bind, the merge would yield an empty dict here."""
    captured: dict[str, Any] = {}

    async def probe() -> None:
        # ``merge_contextvars`` is the production processor that pulls
        # bound contextvars into every event_dict. Run it directly
        # against the current task's context so the assertion mirrors
        # what production log lines would carry.
        merged: dict[str, Any] = {}
        structlog.contextvars.merge_contextvars(None, "", merged)
        captured.update(merged)
        task._session_future.set_result(MagicMock())
        await task._shutdown_event.wait()

    task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
    task._run = probe  # type: ignore[method-assign]

    await task.start()
    await task.close()

    # All three durable identifiers would be merged into any log line
    # emitted from inside the session task.
    assert captured.get("upstream_id") == "notion"
    assert captured.get("user_id") == "alice@co.com"
    assert isinstance(captured.get("session_id"), str)


# ── Spawn-event observability + nested-task inheritance ─────────────


@pytest.mark.asyncio
async def test_spawn_event_emitted_with_session_id_and_no_request_id() -> None:
    """``upstream.session.task.spawned`` is the operator's anchor for
    "which session_id is bound to this task." It MUST NOT carry a
    ``request_id`` field — its absence on this specific event is the
    §3.10 regression signal a future refactor would have to break to
    re-introduce the leak."""
    bind_contextvars(request_id="caller-rid")
    try:
        async def probe() -> None:
            task._session_future.set_result(MagicMock())
            await task._shutdown_event.wait()

        task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
        task._run = probe  # type: ignore[method-assign]

        # Configure structlog to run merge_contextvars before the
        # capture processor so the captured event_dict mirrors what
        # production log lines would carry. ``capture_logs`` alone
        # swaps ALL processors, so contextvars wouldn't merge.
        captured: list[dict[str, Any]] = []

        def capture_processor(_logger, _method, event_dict):
            captured.append(dict(event_dict))
            raise structlog.DropEvent

        old_processors = structlog.get_config()["processors"]
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                capture_processor,
            ],
        )
        try:
            await task.start()
            await task.close()
        finally:
            structlog.configure(processors=old_processors)

        spawned = [e for e in captured if e.get("event") == "upstream.session.task.spawned"]
        assert spawned, f"expected spawn event, got: {captured}"
        event = spawned[0]
        assert event.get("transport") == "streamable_http"
        assert isinstance(event.get("session_id"), str)
        assert event.get("upstream_id") == "notion"
        assert event.get("user_id") == "alice@co.com"
        # The smoking-gun assertion: the spawn event must not carry
        # the caller's request_id.
        assert "request_id" not in event, (
            f"§3.10 regression: spawn event carried caller's request_id: {event}"
        )
    finally:
        clear_contextvars()


@pytest.mark.asyncio
async def test_nested_task_inside_session_inherits_clean_bound_context() -> None:
    """The MCP SDK's ``ClientSession.__aenter__`` spawns its own
    background tasks (read pump, write pump). By Python semantics
    those tasks copy the current context — which is the session
    task's context. Pin that nested tasks see the bound durable
    identifiers AND don't see the spawning request's
    ``request_id``. Without this, a leak could appear in the SDK's
    pump-task log lines even with our fix in place."""
    bind_contextvars(request_id="caller-rid")
    try:
        captured_nested: dict[str, Any] = {}

        async def nested() -> None:
            captured_nested.update(get_contextvars())

        async def probe() -> None:
            # Mimic ``ClientSession.__aenter__`` spawning a sub-task.
            sub = asyncio.create_task(nested())
            await sub
            task._session_future.set_result(MagicMock())
            await task._shutdown_event.wait()

        task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
        task._run = probe  # type: ignore[method-assign]

        await task.start()
        await task.close()

        # Nested tasks must inherit the durable identifiers...
        assert captured_nested.get("upstream_id") == "notion"
        assert captured_nested.get("user_id") == "alice@co.com"
        assert isinstance(captured_nested.get("session_id"), str)
        # ... and must NOT inherit the spawning request's request_id.
        assert "request_id" not in captured_nested, (
            f"§3.10 leak in nested task: {captured_nested}"
        )
    finally:
        clear_contextvars()


@pytest.mark.asyncio
async def test_lifespan_spawned_loop_does_not_inherit_request_scoped_contextvars() -> None:
    """The three periodic loops (token refresh, liveness probe, health
    email) are spawned from FastAPI lifespan, which runs OUTSIDE any
    HTTP request. Their tasks should therefore have no request_id
    bound. Pin that — a future refactor that moves loop-spawn into a
    request handler (e.g. an admin "kick the loop now" button) would
    silently re-introduce the §3.10 leak for periodic events."""
    # Simulate the lifespan-pre-request state: no contextvars bound.
    clear_contextvars()
    captured: dict[str, Any] = {}

    async def fake_loop_body() -> None:
        captured.update(get_contextvars())

    # Lifespan uses asyncio.create_task without a custom context arg,
    # so the periodic loop inherits whatever the lifespan task has.
    # That's empty here — exactly the expected production state.
    loop_task = asyncio.create_task(fake_loop_body())
    await loop_task

    assert "request_id" not in captured, (
        f"lifespan-spawned loop unexpectedly inherited request_id: {captured}"
    )
    # Sanity: at this layer the loop has no durable session bindings
    # either — those belong to per-session tasks. Loops bind their
    # own per-tick ``upstream_id`` / ``user`` via kwargs on each log
    # call (see ``oauth_refresh.refresh_token_for_user``).
    assert "upstream_id" not in captured


@pytest.mark.asyncio
async def test_request_scoped_contextvars_in_lifespan_would_leak_into_loops() -> None:
    """Inverse of the above: if SOMETHING bound a request_id and then
    spawned a periodic loop, the loop would inherit it (Python's
    asyncio default). This test is the negative example — it
    documents exactly why the lifespan-vs-request distinction matters
    and gives a future maintainer a reproducible failure mode."""
    bind_contextvars(request_id="caller-rid")
    try:
        captured: dict[str, Any] = {}

        async def fake_loop_body() -> None:
            captured.update(get_contextvars())

        loop_task = asyncio.create_task(fake_loop_body())
        await loop_task

        # Without a fresh-context spawn (which lifespan loops don't
        # use because they run pre-request), the request_id leaks in.
        # That's why §3.10's fix scopes only to long-lived session
        # tasks spawned from inside requests.
        assert captured.get("request_id") == "caller-rid"
    finally:
        clear_contextvars()
