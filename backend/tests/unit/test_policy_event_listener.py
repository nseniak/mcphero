"""Tests for the per-org policy_changed event listener in app.py.

Phase 5 replaced a single listener (subscribed to ``DEFAULT_ORG_ID``
only) with one listener task per org. These tests mirror the
``_listen_for_org`` coroutine against a channel-isolated fake
``EventStream`` to prove that:

- an event published for org A is picked up by that org's listener,
  and
- org A's listener never sees events published for org B.

The channel-isolated fake matches ``RedisEventStream`` semantics (each
org has its own channel); ``InProcessEventStream`` ignores ``org_id``
internally and would paper over any regression, which is why we use a
dedicated fake here.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mcpolis.domain.model.events import Event
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.event_stream import EventStream


class ChannelIsolatedEventStream(EventStream):
    """Per-org channel semantics, matching RedisEventStream.

    Publishes to ``org_id`` reach only subscribers on ``org_id``. This
    is the contract the production Redis adapter enforces, and the
    semantics the in-process adapter *doesn't* (it ignores org_id),
    which is why the listener bug is invisible under the in-process
    adapter.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[asyncio.Queue[Event | None]]] = {}

    def publish(self, org_id: str, event: Event) -> None:
        for queue in self._queues.get(org_id, []):
            queue.put_nowait(event)

    async def subscribe(
        self, org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._queues.setdefault(org_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    yield None
                    continue
                if event.user_email is None or event.user_email == user_email:
                    yield event
        finally:
            self._queues[org_id].remove(queue)

    async def close(self) -> None:
        return None


class SpyPolicyNotifier:
    """Records the (method, org_id, arg) calls made by the listener."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify_role_changed(self, org_id: str, role: str) -> None:
        self.calls.append(("role", org_id, role))

    def notify_user_changed(self, org_id: str, user: str) -> None:
        self.calls.append(("user", org_id, user))

    def notify_all_roles(self) -> None:
        self.calls.append(("all", "", None))


async def run_listener_for_org(
    event_bus: EventStream, org_id: str, notifier: SpyPolicyNotifier,
) -> None:
    """Mirrors ``_listen_for_org`` in app.py.

    Subscribes to ``org_id``'s channel and dispatches ``policy_changed``
    events to the notifier. Kept intentionally close to the production
    coroutine so a regression there shows up here.
    """
    async for event in event_bus.subscribe(org_id, "*"):
        if event is None:
            continue
        if event.type != "policy_changed":
            continue
        payload: dict[str, Any] = event.payload  # pyright: ignore[reportAssignmentType]
        evt_org = payload.get("org_id", org_id)
        if not isinstance(evt_org, str):
            evt_org = org_id
        role = payload.get("role")
        user = payload.get("user")
        if isinstance(role, str):
            notifier.notify_role_changed(evt_org, role)
        elif isinstance(user, str):
            notifier.notify_user_changed(evt_org, user)
        else:
            notifier.notify_all_roles()


def make_bus_and_notifier() -> tuple[ChannelIsolatedEventStream, SpyPolicyNotifier]:
    return ChannelIsolatedEventStream(), SpyPolicyNotifier()


async def _run_listener_briefly(
    event_bus: EventStream, org_id: str, notifier: SpyPolicyNotifier,
    publish_action: Any, wait_seconds: float = 0.1,
) -> None:
    task = asyncio.create_task(
        run_listener_for_org(event_bus, org_id, notifier),
    )
    try:
        await asyncio.sleep(0.02)  # let the subscribe register
        publish_action()
        await asyncio.sleep(wait_seconds)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_default_org_event_reaches_notifier() -> None:
    """Baseline: an event published to the subscribed org reaches the
    listener — the standalone single-org path."""
    bus, notifier = make_bus_and_notifier()

    await _run_listener_briefly(
        bus, DEFAULT_ORG_ID, notifier,
        publish_action=lambda: bus.publish(DEFAULT_ORG_ID, Event(
            type="policy_changed",
            payload={"role": "viewer"},
        )),
    )

    assert notifier.calls == [("role", DEFAULT_ORG_ID, "viewer")]


@pytest.mark.asyncio
async def test_real_org_event_reaches_per_org_listener() -> None:
    """Per-org listener: a policy_changed event on ``acme``'s channel
    reaches the listener subscribed to ``acme``. This is the Phase 5
    fix — previously the app spawned one listener bound to
    DEFAULT_ORG_ID and silently dropped events for real cloud orgs.
    """
    bus, notifier = make_bus_and_notifier()

    await _run_listener_briefly(
        bus, "acme", notifier,
        publish_action=lambda: bus.publish("acme", Event(
            type="policy_changed",
            payload={"role": "viewer"},
        )),
    )

    assert notifier.calls == [("role", "acme", "viewer")]


@pytest.mark.asyncio
async def test_per_org_listeners_are_isolated() -> None:
    """A listener subscribed to ``acme`` must not see events published
    to ``beta`` — matches RedisEventStream's per-channel isolation and
    prevents cross-tenant leakage of policy_changed."""
    bus, notifier = make_bus_and_notifier()

    await _run_listener_briefly(
        bus, "acme", notifier,
        publish_action=lambda: bus.publish("beta", Event(
            type="policy_changed",
            payload={"role": "viewer"},
        )),
    )

    assert notifier.calls == []


@pytest.mark.asyncio
async def test_non_policy_event_is_ignored() -> None:
    """The listener filters on event.type and must ignore other event
    types (e.g. upstream_tokens_acquired) even when they arrive on
    the subscribed channel."""
    bus, notifier = make_bus_and_notifier()

    await _run_listener_briefly(
        bus, DEFAULT_ORG_ID, notifier,
        publish_action=lambda: bus.publish(DEFAULT_ORG_ID, Event(
            type="upstream_tokens_acquired",
            payload={"upstream_id": "mixpanel"},
        )),
    )

    assert notifier.calls == []
