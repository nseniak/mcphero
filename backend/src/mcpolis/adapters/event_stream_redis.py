"""Redis-backed ``EventStream`` used by cloud mode.

Design notes
------------
* Channel pattern is hardcoded to ``mcpolis:{org_id}:events`` and is
  **never** exposed as a parameter. This is a tenant-isolation guard:
  the adapter refuses wildcard subscriptions so a compromised component
  cannot tail another org's events, and it never calls ``psubscribe``.
* Publish is a synchronous call from domain code (matching the
  in-process adapter's signature), so we schedule the underlying async
  ``redis.publish`` on the running event loop via
  ``asyncio.get_running_loop().create_task``. Fire-and-forget is
  acceptable — if a publish fails we log and drop the event, identical
  to how the in-process adapter drops on a full queue.
* ``subscribe`` yields the same ``Event | None`` shape as the in-process
  stream: real events decoded from the channel, ``None`` as a 15s
  keepalive so long-lived SSE consumers don't look idle to the LB.
* We use ``coredis`` instead of ``redis-py`` because its async surface
  is fully typed for strict pyright. See the note in ``pyproject.toml``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import coredis
import structlog
from coredis.commands.pubsub import PubSub
from coredis.response.types import PubSubMessage

from mcpolis.domain.model.events import Event

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_KEEPALIVE_TIMEOUT = 15.0
_CHANNEL_PREFIX = "mcpolis"
_CHANNEL_SUFFIX = "events"


def _channel_for(org_id: str) -> str:
    if "*" in org_id or "?" in org_id or "[" in org_id:
        raise ValueError(
            f"Refusing to use channel-unsafe org_id {org_id!r} "
            "(no wildcards allowed in channel names)",
        )
    return f"{_CHANNEL_PREFIX}:{org_id}:{_CHANNEL_SUFFIX}"


def _client_from_url(url: str) -> coredis.Redis[str]:
    """Build a str-decoded ``coredis.Redis`` from a ``redis://`` URL.

    coredis 5.x doesn't ship a one-liner URL parser like ``from_url``
    for the generic client, so we unpack the handful of fields we care
    about ourselves.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    db = 0
    if parsed.path and parsed.path != "/":
        try:
            db = int(parsed.path.lstrip("/"))
        except ValueError:
            db = 0
    password = parsed.password
    return coredis.Redis(
        host=host, port=port, db=db, password=password,
        decode_responses=True,
    )


class RedisEventStream:
    """Redis Pub/Sub ``EventStream`` implementation."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: coredis.Redis[str] = _client_from_url(url)
        self._publish_tasks: set[asyncio.Task[None]] = set()

    def publish(self, org_id: str, event: Event) -> None:
        """Fire-and-forget publish via the running event loop."""
        channel = _channel_for(org_id)
        payload = event.model_dump_json()
        logger.info(
            "event_stream.publish",
            event_type=event.type,
            user=event.user_email,
            org_id=org_id,
            channel=channel,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. sync test helper) — skip. Real
            # publishers always run inside the FastAPI event loop.
            logger.warning(
                "event_stream.publish.no_running_loop",
                org_id=org_id,
                channel=channel,
            )
            return
        task = loop.create_task(self._publish(channel, payload))
        # Keep a reference so the task isn't GC'd mid-flight.
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    async def _publish(self, channel: str, payload: str) -> None:
        try:
            await self._client.publish(channel, payload)
        except Exception:
            logger.exception(
                "event_stream.publish.failed",
                channel=channel,
            )

    async def subscribe(
        self, org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]:
        """Async iterator of events targeted at ``user_email``.

        ``user_email == "*"`` is **refused** — the in-process stream uses
        it as a broadcast shorthand, but allowing wildcard fan-out over
        Redis would let one bad subscriber observe every event in the
        org. The policy-event listener that wants broadcast behaviour
        should filter server-side on ``event.user_email is None``.
        """
        if user_email == "*" or "*" in user_email:
            raise ValueError(
                "RedisEventStream does not accept wildcard subscriptions",
            )
        channel = _channel_for(org_id)
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                msg = await _get_message_or_none(pubsub, _KEEPALIVE_TIMEOUT)
                if msg is None:
                    yield None  # keepalive
                    continue
                raw = msg.get("data")
                if not isinstance(raw, (str, bytes)):
                    continue
                try:
                    event = Event.model_validate_json(raw)
                except Exception:
                    logger.exception(
                        "event_stream.subscribe.malformed_event",
                        channel=channel,
                    )
                    continue
                # Fan-out filter: Redis delivers every message on the
                # channel to every subscriber. Apply the same semantics
                # the in-process stream applies: events addressed to a
                # specific user only go to that user's subscribers;
                # broadcasts (user_email=None) go to everyone.
                if event.user_email is None or event.user_email == user_email:
                    yield event
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                logger.exception(
                    "event_stream.unsubscribe.failed",
                    channel=channel,
                )
            try:
                await pubsub.aclose()
            except Exception:
                logger.exception(
                    "event_stream.pubsub_close.failed",
                    channel=channel,
                )

    async def close(self) -> None:
        """Cancel in-flight publishes and close the Redis client."""
        for task in list(self._publish_tasks):
            task.cancel()
        self._publish_tasks.clear()
        # coredis 5.x doesn't expose a public ``aclose``; draining the
        # connection pool is the documented way to tear down.
        try:
            self._client.connection_pool.disconnect()
        except Exception:
            logger.exception("event_stream.client_close.failed")


async def _get_message_or_none(
    pubsub: PubSub[str], timeout: float,
) -> PubSubMessage | None:
    """Return the next real message, or ``None`` when ``timeout`` elapses.

    ``get_message(timeout=…)`` may also return control frames. Loop
    until we see a real ``type == "message"`` frame or ``timeout``
    seconds have elapsed across control traffic.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        msg = await pubsub.get_message(
            ignore_subscribe_messages=True, timeout=remaining,
        )
        if msg is None:
            return None
        if msg.get("type") == "message":
            return msg
