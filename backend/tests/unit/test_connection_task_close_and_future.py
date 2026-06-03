"""ConnectionTask close-timeout cancel + ``_session_future`` rejection.

Two parallel classes — ``HttpConnectionTask`` / ``SandboxConnectionTask``
— share the close + start lifecycle via ``ConnectionTaskBase``. These
tests pin the close-timeout cancel path and the silent-hang rejection
path per-transport so the base class preserves both behaviors and emits
identical log lines (``upstream.{http,sandbox}.close.timeout_cancelling``
with the right structlog fields, including sandbox's ``provider=`` extra).

Pattern follows ``test_upstream_session_contextvars.py``: replace
``_run`` with a probe that controls the session-future / shutdown-
event behavior we want to exercise, then drive ``start()`` / ``close()``
and inspect the result.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

from mcpolis.adapters.upstream_clients.http_adapter import (
    CLOSE_TIMEOUT as HTTP_CLOSE_TIMEOUT,
    HttpConnectionTask,
)
from mcpolis.adapters.upstream_clients.stdio_adapter import (
    SandboxConnectionTask,
)
from mcpolis.domain.model.upstream import TransportType
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition

_ = structlog  # capture_logs imported above; keep structlog as namespace


def _make_http_upstream() -> Any:
    return make_upstream_definition(
        id="notion",
        transport=TransportType.streamable_http,
        url="http://example.invalid/mcp",
    )


def _make_sandbox_task(*, hang: bool) -> SandboxConnectionTask:
    """Build a SandboxConnectionTask wired to a stub service so the
    cancel-on-close path can run without a real E2B SDK call.

    The ``service`` is only consulted via ``self._service.name`` inside
    the close-timeout log line we assert on. We don't use any real
    sandbox lifecycle methods because we override ``_run`` with a
    probe.
    """
    upstream = make_upstream_definition(id="notion", command="echo")
    service = MagicMock()
    service.name = "e2b"
    return SandboxConnectionTask(
        upstream=upstream,
        user_id="bob@co.com",
        service=service,
        resources=SandboxResources(
            cpu_vcpus=1.0, memory_mb=1024, disk_gb=0,
        ),
        org_id="acme",
    )


# ---------- Close-timeout cancel path ----------


@pytest.mark.asyncio
async def test_http_close_timeout_cancels_and_logs() -> None:
    """If the HTTP session task hangs past ``CLOSE_TIMEOUT``,
    ``close()`` must cancel + drain the task, log
    ``upstream.http.close.timeout_cancelling`` with the upstream id +
    ``timeout_seconds``, and return within a small grace of
    ``CLOSE_TIMEOUT``. Pinned because the cancel-on-timeout path is
    the whole reason close() doesn't take ``timeout=None`` — a
    regression that just awaited the task forever would deadlock
    every shutdown.
    """
    task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")

    # Set up the session future + a probe that hangs forever.
    async def hang() -> None:
        task._session_future.set_result(MagicMock())
        await asyncio.Event().wait()  # never set — runs forever

    task._run = hang  # type: ignore[method-assign]

    # Patch CLOSE_TIMEOUT to a small value so the test isn't slow.
    import mcpolis.adapters.upstream_clients.http_adapter as http_adapter
    original_timeout = http_adapter.CLOSE_TIMEOUT
    http_adapter.CLOSE_TIMEOUT = 0.05
    try:
        await task.start()
        with capture_logs() as logs:
            t0 = time.monotonic()
            await task.close()
            elapsed = time.monotonic() - t0

        # Returned within timeout + small grace (cancellation).
        assert elapsed < 0.5, f"close took too long: {elapsed:.3f}s"
        # The underlying task is done.
        assert task._task is not None and task._task.done()
        # Log fired with the right event name + fields.
        events = [
            le for le in logs
            if le.get("event") == "upstream.http.close.timeout_cancelling"
        ]
        assert len(events) == 1, f"expected 1 timeout log, got: {logs}"
        evt = events[0]
        assert evt["upstream_id"] == "notion"
        assert evt["timeout_seconds"] == 0.05
    finally:
        http_adapter.CLOSE_TIMEOUT = original_timeout


@pytest.mark.asyncio
async def test_sandbox_close_timeout_cancels_and_logs_with_provider() -> None:
    """Sandbox is the asymmetric one — its close-timeout log line
    carries an extra ``provider=`` field (the SandboxService backend
    name). Operators' Splunk queries on the sandbox events depend on
    that field. Pinned now so the upcoming ConnectionTaskBase MUST
    preserve the extra field via a per-subclass ``_log_extras`` hook
    rather than collapsing every transport's event to the same shape.
    """
    task = _make_sandbox_task(hang=True)

    async def hang() -> None:
        task._session_future.set_result(MagicMock())
        await asyncio.Event().wait()

    task._run = hang  # type: ignore[method-assign]

    import mcpolis.adapters.upstream_clients.stdio_adapter as stdio_adapter
    original_timeout = stdio_adapter.CLOSE_TIMEOUT
    stdio_adapter.CLOSE_TIMEOUT = 0.05
    try:
        await task.start()
        with capture_logs() as logs:
            t0 = time.monotonic()
            await task.close()
            elapsed = time.monotonic() - t0
        assert elapsed < 0.5
        assert task._task is not None and task._task.done()
        events = [
            le for le in logs
            if le.get("event") == "upstream.sandbox.close.timeout_cancelling"
        ]
        assert len(events) == 1, f"expected 1 timeout log, got: {logs}"
        evt = events[0]
        assert evt["upstream_id"] == "notion"
        assert evt["timeout_seconds"] == 0.05
        # The sandbox-specific extra field.
        assert evt["provider"] == "e2b", (
            f"sandbox close-timeout log must carry provider=, got: {evt}"
        )
    finally:
        stdio_adapter.CLOSE_TIMEOUT = original_timeout


# ---------- ``_session_future`` rejection path ----------
#
# Each ``_run`` wraps its inner stream-acquisition + ClientSession
# block in ``try/except Exception`` and calls ``self._session_future
# .set_exception(exc)`` if the future hasn't been resolved yet. Without
# that wrap, ``await self._session_future`` inside ``start()`` would
# hang forever — the silent-hang failure mode that bit prior
# sessions. These tests patch the inner SDK call (``streamable_http
# _client`` / ``stdio_client`` / ``service.session``) to raise BEFORE
# ``set_result`` runs, then assert ``start()`` propagates the
# exception via the future.


@pytest.mark.asyncio
async def test_http_session_future_rejection_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``streamable_http_client`` raising before ``set_result`` must
    surface through ``await task.start()``. A regression that drops
    the ``except Exception → set_exception`` wrap in ``_run`` would
    deadlock here."""

    class BootError(RuntimeError):
        pass

    def boom(**_: Any) -> Any:
        raise BootError("stream acquisition blew up")

    monkeypatch.setattr(
        "mcpolis.adapters.upstream_clients.http_adapter.streamable_http_client",
        boom,
    )

    task = HttpConnectionTask(_make_http_upstream(), user_id="alice@co.com")
    with pytest.raises(BootError):
        await asyncio.wait_for(task.start(), timeout=2.0)


@pytest.mark.asyncio
async def test_sandbox_session_future_rejection_propagates() -> None:
    """Same silent-hang guard for sandbox — make
    ``service.session(...)`` raise on entry. In production this fires
    on E2B sandbox creation timeouts and template-grid lookup
    misses."""

    class BootError(RuntimeError):
        pass

    class _BoomSessionContext:
        async def __aenter__(self) -> Any:
            raise BootError("sandbox.session entry failed")

        async def __aexit__(self, *args: Any) -> None:
            return None

    task = _make_sandbox_task(hang=False)
    # ``_make_sandbox_task`` wired ``service`` as a MagicMock; replace
    # ``service.session`` so the real ``_run`` enters the failing
    # context manager.
    task._service.session = MagicMock(  # type: ignore[attr-defined]
        return_value=_BoomSessionContext(),
    )

    with pytest.raises(BootError):
        await asyncio.wait_for(task.start(), timeout=2.0)


# Sanity: the imported CLOSE_TIMEOUT constant exists and has the
# documented default. Catches a drift where a refactor renames or
# moves it without updating this file.
def test_close_timeout_constants_present() -> None:
    assert HTTP_CLOSE_TIMEOUT == 10.0
