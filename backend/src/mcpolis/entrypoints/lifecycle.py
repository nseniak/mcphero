"""Graceful drain coordinator for horizontal scaling.

When SIGTERM is received the backend enters *draining* mode:

1. ``/healthz`` returns ``{"status": "draining"}`` so the load balancer
   stops routing new sessions to this instance.
2. In-flight requests continue to run for up to ``drain_timeout`` seconds.
3. New requests receive a 503 Service Unavailable.
4. Once all in-flight requests finish (or the timeout expires), the
   lifespan teardown proceeds normally.

In standalone mode the drain coordinator still works — it just means a
clean ``kill <pid>`` instead of an abrupt ``SIGKILL``.
"""
from __future__ import annotations

import asyncio

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class DrainCoordinator:
    """Tracks active requests and coordinates graceful shutdown."""

    def __init__(self, drain_timeout: float = 30.0) -> None:
        self._drain_timeout = drain_timeout
        self._draining = False
        self._active_requests = 0
        self._drain_complete: asyncio.Event = asyncio.Event()

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def request_started(self) -> None:
        self._active_requests += 1

    def request_finished(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        if self._draining and self._active_requests == 0:
            self._drain_complete.set()

    async def drain(self) -> None:
        """Enter draining mode and wait for in-flight requests to finish.

        Returns immediately if there are no active requests. Otherwise
        waits up to ``drain_timeout`` seconds.
        """
        self._draining = True
        logger.info(
            "lifecycle.drain.started",
            active_requests=self._active_requests,
            drain_timeout_seconds=self._drain_timeout,
        )

        if self._active_requests == 0:
            logger.info("lifecycle.drain.complete_immediately")
            return

        try:
            await asyncio.wait_for(
                self._drain_complete.wait(),
                timeout=self._drain_timeout,
            )
            logger.info("lifecycle.drain.complete")
        except TimeoutError:
            logger.warning(
                "lifecycle.drain.timeout",
                active_requests=self._active_requests,
            )
