"""EventStream port — pub/sub of real-time events to the dashboard.

Implementations:
- ``InProcessEventStream`` (standalone mode): per-subscriber asyncio queues.
- ``RedisEventStream`` (cloud mode): Redis Pub/Sub, one channel per org.

Consumers only see this protocol. Deliberately minimal — no topic
negotiation, no wildcard subscription, no message replay. The Redis
adapter hardcodes the channel pattern ``mcpolis:{org_id}:events`` and
refuses wildcard user subscriptions so a compromised component cannot
tail another org's events.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from mcpolis.domain.model.events import Event


class EventStream(Protocol):
    def publish(self, org_id: str, event: Event) -> None: ...

    def subscribe(
        self, org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]: ...

    async def close(self) -> None: ...
