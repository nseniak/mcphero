"""AUTH-15 — concurrent ``_maybe_touch`` throttle race.

``ServiceTokenService.verify`` updates ``last_used_at`` at most once
per :data:`LAST_USED_WRITE_INTERVAL_SECONDS` per token, gated by an
in-memory ``_last_touch`` dict. Two requests for the same token can
race the gate. This module pins:

1. The race is *benign*: gathered verifies of one fresh token produce
   at most two touch writes (never a storm, never an exception). The
   gate's check-then-set is synchronous (no await between the
   ``_last_touch.get`` read and the write), so the only interleaving
   point is the awaited ``touch_last_used`` — by which time the dict
   is already set, so a second concurrent verify is throttled.

2. ``_last_touch`` growth is bounded by the count of *distinct*
   tokens (one entry per token_hash), not by request volume. Repeated
   verifies of the same token keep the dict at a single entry. The
   live service-token population is small and operator-controlled, so
   this is not an unbounded-growth resource gap.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from mcpolis.domain.model.service_token import ServiceTokenRecord
from mcpolis.domain.services.service_token_service import ServiceTokenService

from tests.unit.factories import make_service_token_record


class _WriteCountingRepo:
    """Repo double: always finds one fixed record, counts touch writes,
    and yields inside ``touch_last_used`` to widen the race window."""

    def __init__(self, record: ServiceTokenRecord) -> None:
        self._record = record
        self.get_calls: int = 0
        self.touch_calls: int = 0

    async def get_by_hash(self, token_hash: str) -> ServiceTokenRecord | None:
        self.get_calls += 1
        # Yield so two gathered verifies interleave before either
        # reaches the throttle gate.
        await asyncio.sleep(0)
        if token_hash == self._record.token_hash:
            return self._record
        return None

    async def touch_last_used(self, token_hash: str, when: datetime) -> None:
        self.touch_calls += 1
        # Yield mid-write so a racing verify can observe the gate state
        # the first writer already set.
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_verify_touches_at_most_twice_no_exception() -> None:
    """AUTH-15: two concurrent verifies of the same fresh token write
    ``last_used_at`` at most twice (the benign-race bound) and neither
    raises. ``monotonic`` is pinned constant so the throttle window is
    fully open — any write past the second would be a real storm."""
    record = make_service_token_record(raw_token="svct_race")
    repo = _WriteCountingRepo(record)
    service = ServiceTokenService(
        repo=repo,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        monotonic=lambda: 1000.0,
    )

    raw = "svct_race"
    results = await asyncio.gather(
        service.verify(raw),
        service.verify(raw),
    )
    # Both resolve to the same record — no exception leaked through.
    assert all(r is not None and r.token_hash == record.token_hash
               for r in results)
    # Benign-race bound: at most two writes, never a storm. In practice
    # single-threaded asyncio serializes the two ``verify`` coroutines (the
    # read-check-write in ``_maybe_touch`` has no await between the check
    # and the write), so the observed value is 1 today; ``<= 2`` is the
    # safety ceiling that still holds if a future await split lets both
    # observe ``last is None`` before either writes. The real coverage here
    # is "concurrent verify never raises and never storms"; the exact write
    # count is pinned by the resource-model tests below.
    assert repo.touch_calls <= 2
    # And at least one write happened (a fresh token must be touched).
    assert repo.touch_calls >= 1


@pytest.mark.asyncio
async def test_repeated_verify_keeps_last_touch_bounded_by_distinct_tokens(
) -> None:
    """AUTH-15 (resource): ``_last_touch`` is keyed by token_hash, so
    verifying ONE token many times keeps it at a single entry — growth
    tracks the distinct-token population, not request count. Not an
    unbounded-growth gap (the live token set is small and
    operator-controlled)."""
    record = make_service_token_record(raw_token="svct_repeat")
    repo = _WriteCountingRepo(record)
    service = ServiceTokenService(
        repo=repo,  # type: ignore[arg-type]
        now=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        monotonic=lambda: 1000.0,
    )

    for _ in range(50):
        assert await service.verify("svct_repeat") is not None

    # One token verified 50 times → exactly one throttle-state entry.
    assert len(service._last_touch) == 1  # pyright: ignore[reportPrivateUsage]
    assert record.token_hash in service._last_touch  # pyright: ignore[reportPrivateUsage]
    # With the window held open, the first verify writes once and the
    # rest are throttled.
    assert repo.touch_calls == 1


@pytest.mark.asyncio
async def test_last_touch_grows_one_entry_per_distinct_token() -> None:
    """AUTH-15 (resource): N distinct tokens → N entries; the dict is
    keyed by hash, so the bound is the distinct-token count. Pins the
    growth model the resource note above relies on."""
    raws = [f"svct_tok-{i}" for i in range(5)]
    records = {
        raw: make_service_token_record(label=f"bot-{i}", raw_token=raw)
        for i, raw in enumerate(raws)
    }

    class _MultiRecordRepo:
        def __init__(self) -> None:
            self.touch_calls = 0

        async def get_by_hash(
            self, token_hash: str,
        ) -> ServiceTokenRecord | None:
            for rec in records.values():
                if rec.token_hash == token_hash:
                    return rec
            return None

        async def touch_last_used(
            self, token_hash: str, when: datetime,
        ) -> None:
            self.touch_calls += 1

    service = ServiceTokenService(
        repo=_MultiRecordRepo(),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        monotonic=lambda: 1000.0,
    )
    for raw in raws:
        assert await service.verify(raw) is not None

    assert len(service._last_touch) == 5  # pyright: ignore[reportPrivateUsage]
