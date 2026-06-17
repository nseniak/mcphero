"""Sentry initialization with user/org enrichment and header scrubbing.

Opt-in: empty ``MCPOLIS_SENTRY_DSN`` disables Sentry entirely
(``init_sentry`` becomes a no-op). When enabled, every outgoing event
is enriched with the current user/org and auth-bearing headers are
scrubbed.

Identity enrichment is deliberately careful about *which* source it
reads, because two coexist and they don't always agree:

* The gateway ContextVars ``current_user_id`` / ``current_org_id``.
  Only the dashboard-cookie middleware (``user_identity_middleware``)
  ever writes a real value into ``current_user_id``; OAuth/MCP traffic
  and detached background tasks leave it at its ``"anonymous"`` default.
* structlog's bound contextvars (``user_id`` / ``org_id``), bound by
  ``_MinimalLoggingMiddleware`` from the OAuth bearer identity and
  propagated into ``asyncio.create_task`` background work (the context
  is copied at task-creation time).

Reading only the ContextVars made every OAuth/MCP event and every
background-task event report ``email: anonymous`` even when the real
user was known — e.g. MCPOLIS-BACKEND-P, a tool-registry refresh
``TimeoutError`` that carried the real ``user_id`` in its log payload
yet showed ``email: anonymous`` in Sentry. We now prefer whichever
source yields a *real* (non-sentinel) value, and never write the
sentinel itself into the ``email`` field or ``org_id`` tag.
"""

from __future__ import annotations

from collections.abc import Callable

import sentry_sdk
import structlog
from structlog.contextvars import get_contextvars
from sentry_sdk.types import Event, Hint

from mcpolis.domain.ports import DEFAULT_ORG_ID, MULTI_ORG_SENTINEL
from mcpolis.entrypoints.config import Mode, Settings
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_SCRUB_HEADERS = {"authorization", "cookie"}

# ``current_user_id`` defaults to (and the dashboard middleware writes)
# this string when no real identity is known. It is never a real email,
# so it must not be stuffed into Sentry's ``user.email`` field.
_ANON_USER = "anonymous"

# Paths hit only by machine probes (the Docker/LB healthchecks). The
# cloud compose runs a 10s ``/health`` healthcheck against the ASGI app
# — ~8.6k requests/day — and the FastAPI integration would auto-trace
# every one. A sibling project sharing this Sentry org's pooled
# transaction quota let exactly this become ~90% of its transactions
# and starved the quota, rate-limiting every other project (mcpolis
# included). We sample these to 0.0 so the probe volume can never spend
# quota, independent of how often it fires.
_UNTRACED_PATHS = frozenset({"/health", "/healthz"})


def _first_real(
    candidates: tuple[object, ...], sentinels: frozenset[str]
) -> str | None:
    """First candidate that is a non-empty string and not a sentinel."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in sentinels:
            return candidate
    return None


def _org_sentinels(mode: Mode) -> frozenset[str]:
    """Org values that mean "no concrete org was resolved".

    In standalone mode ``DEFAULT_ORG_ID`` is the single real org, so it
    is NOT a sentinel; in cloud it means the org context was never
    established (e.g. a background task running in the default context).
    """
    if mode == "cloud":
        return frozenset({DEFAULT_ORG_ID, MULTI_ORG_SENTINEL})
    return frozenset({MULTI_ORG_SENTINEL})


def _make_before_send(
    org_sentinels: frozenset[str],
) -> Callable[[Event, Hint], Event | None]:
    def _before_send(event: Event, _hint: Hint) -> Event | None:
        # Identity is read at event-send time (when the exception/log
        # fires), preferring whichever source carries a real value:
        # structlog's bound contextvars are the OAuth/MCP + background-task
        # source; the gateway ContextVars are the dashboard-cookie source.
        bound = get_contextvars()

        email = _first_real(
            (current_user_id.get(), bound.get("user_id")),
            frozenset({_ANON_USER}),
        )
        if email:
            user = event.get("user") or {}
            user.setdefault("email", email)
            event["user"] = user

        org_id = _first_real(
            (bound.get("org_id"), current_org_id.get()),
            org_sentinels,
        )
        if org_id:
            tags = event.get("tags") or {}
            if isinstance(tags, dict):
                tags.setdefault("org_id", org_id)
                event["tags"] = tags

        # Scrub auth-bearing headers.
        request = event.get("request")
        if isinstance(request, dict):
            headers_obj: object = request.get("headers")
            if isinstance(headers_obj, dict):
                scrubbed: dict[str, str] = {}
                for raw_key, raw_value in headers_obj.items():  # type: ignore[reportUnknownVariableType]
                    if isinstance(raw_key, str):
                        if raw_key.lower() in _SCRUB_HEADERS:
                            scrubbed[raw_key] = "[Filtered]"
                        elif isinstance(raw_value, str):
                            scrubbed[raw_key] = raw_value
                request["headers"] = scrubbed

        return event

    return _before_send


def _make_traces_sampler(
    base_rate: float, untraced_paths: frozenset[str]
) -> Callable[[dict[str, object]], float]:
    """Sample at ``base_rate`` except for machine-probe paths (→ 0.0).

    The ASGI integration exposes the request path at
    ``sampling_context["asgi_scope"]["path"]``. We deliberately do NOT
    honor an inbound ``parent_sampled`` decision: the ``/mcp`` gateway
    is public, so trusting a client-supplied ``sentry-trace`` header
    would let anyone force 100% sampling and burn the shared quota.
    """

    def _traces_sampler(sampling_context: dict[str, object]) -> float:
        scope = sampling_context.get("asgi_scope")
        if isinstance(scope, dict):
            path = scope.get("path")  # type: ignore[reportUnknownMemberType]
            if isinstance(path, str) and path in untraced_paths:
                return 0.0
        return base_rate

    return _traces_sampler


def init_sentry(settings: Settings) -> bool:
    """Initialize Sentry if a DSN is configured. Returns True if enabled."""
    if not settings.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.mode,
        release=settings.release or None,
        traces_sampler=_make_traces_sampler(
            settings.sentry_traces_sample_rate, _UNTRACED_PATHS
        ),
        send_default_pii=False,
        before_send=_make_before_send(_org_sentinels(settings.mode)),
    )
    logger.info(
        "sentry.enabled",
        environment=settings.sentry_environment or settings.mode,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        untraced_paths=sorted(_UNTRACED_PATHS),
    )
    return True
