"""Tests for the Phase 2d ``EventStream`` adapters.

Covers:
- In-process stream: user-specific vs broadcast delivery, close is a no-op.
- Redis stream: cross-subscriber delivery (simulating cross-process),
  tenant isolation via org-scoped channels, rejection of wildcard
  subscriptions.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

import pytest

from mcpolis.adapters.event_stream_inprocess import InProcessEventStream
from mcpolis.adapters.event_stream_redis import RedisEventStream, _channel_for
from mcpolis.domain.model.events import Event

from tests.unit.redis_fixture import redis_available, require_redis


# ── In-process stream ─────────────────────────────────────────────────


async def test_inprocess_user_specific_delivery() -> None:
    stream = InProcessEventStream()

    received: list[Event] = []

    async def collect() -> None:
        async for ev in stream.subscribe("default", "alice@test.com"):
            if ev is not None:
                received.append(ev)
                return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)

    stream.publish("default", Event(
        type="upstream_tokens_acquired",
        user_email="alice@test.com",
        payload={"upstream_id": "mixpanel"},
    ))

    await asyncio.wait_for(task, timeout=1.0)
    assert len(received) == 1
    assert received[0].type == "upstream_tokens_acquired"
    await stream.close()  # no-op, should not raise


async def test_inprocess_close_is_noop() -> None:
    stream = InProcessEventStream()
    await stream.close()
    # Publishing after close still works — close is a no-op for the
    # in-process stream. This matches the contract: close releases
    # external resources, and there are none.
    stream.publish("default", Event(type="noop", payload={}))


# ── Redis stream ──────────────────────────────────────────────────────


def _skip_unless_redis() -> None:
    if not redis_available():
        pytest.skip("Redis not reachable — start the dev compose profile")


async def _publish_until(
    publisher: RedisEventStream,
    org: str,
    event: Event,
    *,
    until: Callable[[], bool],
    deadline_s: float = 15.0,
) -> None:
    """Re-publish ``event`` until ``until()`` holds (or the deadline).

    Redis pub/sub drops messages published before a subscriber's
    SUBSCRIBE has registered. The old tests slept a fixed 0.2 s before
    a single publish and hoped both subscriber tasks had been scheduled
    and finished their SUBSCRIBE round-trip by then. Under ``make
    test-all`` CPU starvation that wasn't always true — the publish
    raced ahead of the subscribe and the event was lost *forever* (no
    receive-deadline could recover it; the symptom was a
    ``CancelledError`` when the 3 s ``wait_for`` gave up). Re-publishing
    on a short cadence until delivery removes the race entirely without
    depending on any wall-clock guess. Subscribers return after their
    first event, so extra publishes land on a closed generator and are
    harmlessly dropped — the ``== 1`` inbox assertions still hold.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while not until() and loop.time() < deadline:
        publisher.publish(org, event)
        await asyncio.sleep(0.1)


async def test_redis_channel_pattern_is_org_scoped() -> None:
    assert _channel_for("acme") == "mcpolis:acme:events"
    assert _channel_for("widgets") == "mcpolis:widgets:events"


async def test_redis_refuses_wildcard_org_id() -> None:
    with pytest.raises(ValueError):
        _channel_for("*")
    with pytest.raises(ValueError):
        _channel_for("evil?")


async def test_redis_refuses_wildcard_user_subscription() -> None:
    _skip_unless_redis()
    url = require_redis()
    stream = RedisEventStream(url)
    try:
        with pytest.raises(ValueError):
            # Awaiting the iterator's first step materialises the
            # generator body, where the check lives.
            gen = stream.subscribe("default", "*")
            await anext(gen)
    finally:
        await stream.close()


async def test_redis_cross_subscriber_delivery() -> None:
    """Two subscribers on the same org channel both see a broadcast event.

    This is the property that makes the Redis adapter useful in a
    horizontally-scaled deployment: the publishing backend doesn't
    need a direct handle to every subscriber's in-memory queue —
    subscription is mediated by Redis.
    """
    _skip_unless_redis()
    url = require_redis()

    # One publisher client, two subscriber clients — simulates three
    # separate backend processes on the same Redis.
    publisher = RedisEventStream(url)
    subscriber_a = RedisEventStream(url)
    subscriber_b = RedisEventStream(url)

    org = f"test-{uuid.uuid4().hex[:8]}"
    received_a: list[Event] = []
    received_b: list[Event] = []

    async def collect(
        stream: RedisEventStream, email: str, target: list[Event],
    ) -> None:
        async for ev in stream.subscribe(org, email):
            if ev is not None:
                target.append(ev)
                return

    task_a = asyncio.create_task(
        collect(subscriber_a, "alice@test.com", received_a),
    )
    task_b = asyncio.create_task(
        collect(subscriber_b, "bob@test.com", received_b),
    )

    # Re-publish the broadcast until both inboxes fill, so a slow
    # SUBSCRIBE under load can't drop the lone publish (see
    # ``_publish_until``). A subscriber returns after its first event,
    # so each inbox still ends with exactly one.
    broadcast = Event(
        type="policy_changed",
        user_email=None,  # broadcast
        payload={"role": "admin"},
    )
    try:
        await _publish_until(
            publisher, org, broadcast,
            until=lambda: bool(received_a) and bool(received_b),
        )
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)
    finally:
        await publisher.close()
        await subscriber_a.close()
        await subscriber_b.close()

    assert len(received_a) == 1
    assert received_a[0].type == "policy_changed"
    assert len(received_b) == 1
    assert received_b[0].type == "policy_changed"


async def test_redis_user_scoped_delivery_filtered() -> None:
    """Events targeted at alice should not reach bob, even though both
    subscribers share the same org channel."""
    _skip_unless_redis()
    url = require_redis()

    publisher = RedisEventStream(url)
    alice_sub = RedisEventStream(url)
    bob_sub = RedisEventStream(url)

    org = f"test-{uuid.uuid4().hex[:8]}"
    alice_received: list[Event] = []
    bob_received: list[Event] = []

    async def collect(
        stream: RedisEventStream, email: str, target: list[Event],
    ) -> None:
        async for ev in stream.subscribe(org, email):
            if ev is not None:
                target.append(ev)
                return

    task_alice = asyncio.create_task(
        collect(alice_sub, "alice@test.com", alice_received),
    )
    task_bob = asyncio.create_task(
        collect(bob_sub, "bob@test.com", bob_received),
    )

    # Re-publish alice's event until she receives it (see
    # ``_publish_until``). The event targets alice, so bob is filtered
    # out server-side no matter how many times we publish.
    alice_event = Event(
        type="upstream_tokens_acquired",
        user_email="alice@test.com",
        payload={"upstream_id": "mixpanel"},
    )
    try:
        await _publish_until(
            publisher, org, alice_event,
            until=lambda: bool(alice_received),
        )
        await asyncio.wait_for(task_alice, timeout=5.0)
    finally:
        # Bob's task will never complete — cancel it and assert his
        # inbox is empty.
        await asyncio.sleep(0.2)
        task_bob.cancel()
        try:
            await task_bob
        except asyncio.CancelledError:
            pass
        await publisher.close()
        await alice_sub.close()
        await bob_sub.close()

    assert len(alice_received) == 1
    assert alice_received[0].user_email == "alice@test.com"
    assert bob_received == []
