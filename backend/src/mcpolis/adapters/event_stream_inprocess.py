"""In-process ``EventStream`` used by standalone mode.

Delivers events to same-process subscribers via per-subscriber asyncio
queues. No network hop, no serialisation. This was the only event
delivery mechanism pre-Phase-2d, when it was named ``EventBus``;
Phase 2d split the protocol out so cloud mode can use Redis Pub/Sub.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import structlog

from mcpolis.domain.model.events import Event

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_KEEPALIVE_TIMEOUT = 15.0


class InProcessEventStream:
    """Publish/subscribe event stream backed by asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[Event | None]]] = {}

    def publish(self, org_id: str, event: Event) -> None:
        """Deliver event to matching subscribers (user-specific + broadcast)."""
        logger.info(
            "event_stream.publish",
            event_type=event.type,
            user=event.user_email,
            org_id=org_id,
            subscribers=list(self._subscribers.keys()),
        )
        targets: list[asyncio.Queue[Event | None]] = []
        if event.user_email is not None:
            targets.extend(self._subscribers.get(event.user_email, []))
            # Also deliver to broadcast subscribers
            targets.extend(self._subscribers.get("*", []))
        else:
            # Broadcast to all subscribers
            for queues in self._subscribers.values():
                targets.extend(queues)
        for q in targets:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "event_stream.publish.queue_full",
                    event_type=event.type,
                    user=event.user_email,
                    org_id=org_id,
                )

    async def subscribe(
        self, org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]:
        """Async generator yielding events for a user. Yields None as keepalive."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=64)
        subs = self._subscribers.setdefault(user_email, [])
        subs.append(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_KEEPALIVE_TIMEOUT,
                    )
                    yield event
                except TimeoutError:
                    yield None  # keepalive
        finally:
            subs.remove(queue)
            if not subs:
                self._subscribers.pop(user_email, None)

    async def close(self) -> None:
        """No-op — in-process stream owns no external resources."""
        return None
