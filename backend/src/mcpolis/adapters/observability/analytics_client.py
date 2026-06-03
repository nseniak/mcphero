"""Fire-and-forget Mixpanel client for backend events.

Env-driven: an empty ``MCPOLIS_MIXPANEL_TOKEN`` makes every ``track``
call a no-op, so standalone dev installs never hit the network.

Mixpanel's Python SDK is synchronous and blocking (it POSTs inside
``track``). We wrap every call in ``asyncio.create_task`` + a thread
executor so request latency isn't affected even when Mixpanel is slow.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import structlog
from mixpanel import Consumer, Mixpanel

from mcpolis.entrypoints.config import Settings

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class AnalyticsClient:
    """Thin wrapper around ``mixpanel.Mixpanel`` with async fan-out.

    ``track_async`` schedules the POST on the default executor so the
    caller never awaits a network round-trip. Errors are logged and
    swallowed — analytics must never break a user-facing request.
    """

    def __init__(
        self,
        token: str,
        super_properties: dict[str, Any],
        api_host: str = "",
    ) -> None:
        self._enabled = bool(token)
        if token:
            # Pass a Consumer with the EU host when configured; default
            # Consumer hits the US endpoint. Request timeout keeps the
            # executor thread from hanging on Mixpanel outages.
            if api_host:
                consumer = Consumer(api_host=api_host, request_timeout=5)
            else:
                consumer = Consumer(request_timeout=5)
            self._client: Mixpanel | None = Mixpanel(token, consumer=consumer)  # type: ignore[reportUnknownArgumentType]
        else:
            self._client = None
        self._super_properties = super_properties

    @property
    def enabled(self) -> bool:
        return self._enabled

    def track_async(
        self,
        distinct_id: str,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if not self._enabled or self._client is None:
            return
        merged = {**self._super_properties, **(properties or {})}
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._track_sync, distinct_id, event, merged)

    def _track_sync(
        self,
        distinct_id: str,
        event: str,
        properties: dict[str, Any],
    ) -> None:
        try:
            assert self._client is not None
            # mixpanel-python doesn't ship type stubs; the wrapper is the
            # only place we need to silence pyright.
            self._client.track(distinct_id, event, properties)  # type: ignore[reportUnknownMemberType]
        except Exception:
            logger.warning(
                "analytics.mixpanel.track.failed",
                mixpanel_event=event,
                exc_info=True,
            )


# Module-level singleton. Exposed via ``get_analytics()`` so deep call
# sites (e.g. ``tool_router``) can emit events without rethreading the
# dependency through every factory. Tests can call ``set_analytics`` to
# override or reset — defaults to a disabled client so unawared callers
# get a safe no-op.
_singleton: AnalyticsClient = AnalyticsClient(token="", super_properties={})


def set_analytics(client: AnalyticsClient) -> None:
    global _singleton
    _singleton = client


def get_analytics() -> AnalyticsClient:
    return _singleton


def email_hash(email: str) -> str:
    """SHA256 of an email, used for target_email_hash on user_* events.

    Keeps one user's events from leaking another user's raw address
    while still letting funnels count unique invites/removals.
    """
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()


def build_analytics_client(settings: Settings) -> AnalyticsClient:
    super_properties: dict[str, Any] = {
        "mode": settings.mode,
        "app_environment": settings.sentry_environment or settings.mode,
    }
    if settings.release:
        super_properties["release"] = settings.release
    client = AnalyticsClient(
        settings.mixpanel_token,
        super_properties,
        settings.mixpanel_api_host,
    )
    if client.enabled:
        logger.info(
            "analytics.mixpanel.enabled",
            mode=settings.mode,
            api_host=settings.mixpanel_api_host or "default",
        )
    return client
