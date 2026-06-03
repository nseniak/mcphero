"""§5.5 liveness probe for upstream OAuth sessions.

``is_connected(upstream_id)`` is a pure dict lookup over
``UpstreamClientManager``'s session dicts. A session whose
underlying transport has long since idle-closed server-side, or
whose refresh token has quietly gone bad upstream, still shows
"connected" until the next real tool call. On 2026-04-24 a
restart surprised us with a Mixpanel session that had been dead
for ≥12h — the UI had been lying because no one had exercised
the session in that window.

This module runs ``list_tools()`` against every live OAuth
session at ``HEALTH_CHECK_INTERVAL`` cadence. Dead sessions are
torn down and fed back through ``reconnect_with_stored_tokens``,
which applies §5.1's delete-vs-retry policy (so we don't
re-implement it here).

Split out of ``upstream_connection_service.py`` during the
post-§5.2 cleanup. Test monkeypatches that previously targeted
``upstream_connection_service.probe_upstream_liveness`` etc. now
address this module directly — see ``test_liveness_probe.py``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from enum import StrEnum

import httpx
import structlog
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.upstream import UpstreamDefinition


logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# Hourly by default — below that, we'd pin real bandwidth against
# providers like Notion for no forensic benefit, and above that, a
# zombie session (transport idle-closed server-side while our dict
# still shows "connected") would be invisible for too long.
HEALTH_CHECK_INTERVAL = 60 * 60
HEALTH_CHECK_PROBE_TIMEOUT = 15


class ProbeOutcome(StrEnum):
    """Outcome classification for one ``probe_upstream_liveness`` call.

    Returned so the surrounding loop can aggregate — without this
    (pre-Gap C), an operator reading prod logs had to scan individual
    DEBUG probe lines to count healthy vs torn-down outcomes per
    tick, which doesn't scale and doesn't alert.
    """
    healthy = "healthy"
    transient = "transient"
    torn_down = "torn_down"


def _unwrap_error(e: BaseException) -> str:
    """Extract a readable message from exceptions, unwrapping ExceptionGroups.

    Duplicated from ``upstream_connection_service`` to keep the
    liveness module free of reverse-dependencies on the rest of the
    OAuth-durability surface."""
    subs: tuple[BaseException, ...] = getattr(e, "exceptions", ())
    if subs:
        return _unwrap_error(subs[0])
    return str(e)


async def probe_upstream_liveness(
    org_id: str,
    upstream: UpstreamDefinition,
    user_id: str,
    session: ClientSession,
    client_manager: UpstreamClientManager,
    connection_store: ConnectionStore,
    server_url: str,
) -> ProbeOutcome:
    """Run ``list_tools()`` against one live session; on any failure
    other than a transient timeout or transport error, tear down the
    session and let ``reconnect_with_stored_tokens`` apply §5.1's
    delete-vs-retry policy.

    Three outcomes:

    - Success → session is alive, nothing to do.
    - ``asyncio.TimeoutError`` / ``httpx.ConnectError`` / similar
      transport-level blip → leave the session alone. A brief network
      glitch during the probe shouldn't tear down a session that's
      otherwise healthy; the next probe or next real tool call will
      retry. Logged at DEBUG to keep prod logs readable.
    - Any other exception (auth rejection, MCP-level error) → the
      session's underlying state is compromised. Tear it down, then
      attempt a stored-token reconnect. A successful reconnect
      restores the session transparently; a genuine ``invalid_grant``
      there hits §5.1's delete path.

    Keeps log output consistent with the ``Created/Closed admin session``
    bracket so operators can correlate: a probe-triggered teardown
    emits ``Liveness probe torn down upstream=… user=… reason=…``
    immediately before the standard ``Closed`` line.
    """
    # Local import to break the cycle: the reconnect module depends on
    # the provider primitives, which are in the service module; the
    # liveness module feeds results back into reconnect. Keeping the
    # import local avoids a top-level cycle.
    from mcpolis.domain.services.upstream_connection_service import (
        reconnect_with_stored_tokens,
    )

    try:
        await asyncio.wait_for(
            session.list_tools(),
            timeout=HEALTH_CHECK_PROBE_TIMEOUT,
        )
        logger.debug(
            "upstream.health.probe.ok",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        return ProbeOutcome.healthy
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug(
            "upstream.health.probe.timeout",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
        return ProbeOutcome.transient
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
        logger.debug(
            "upstream.health.probe.transport_error",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )
        return ProbeOutcome.transient
    except Exception as exc:
        logger.info(
            "upstream.health.probe.torn_down",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
            exception_type=type(exc).__name__,
            exception_message=_unwrap_error(exc),
        )

    # Session is compromised. Tear down; then let the standard
    # reconnect path handle delete-vs-retry via §5.1.
    await client_manager.disconnect_user_session(upstream.id, user_id)

    reason = await reconnect_with_stored_tokens(
        org_id, upstream, user_id,
        connection_store, client_manager, server_url,
    )
    if reason is None:
        logger.info(
            "upstream.health.probe.recovered",
            upstream_id=upstream.id,
            user=user_id,
            org_id=org_id,
        )
    return ProbeOutcome.torn_down


async def run_liveness_probes_for_org(
    org_id: str,
    upstreams: list[UpstreamDefinition],
    connection_store: ConnectionStore,
    client_manager: UpstreamClientManager,
    server_url: str,
) -> dict[str, int]:
    """One probe pass across every live OAuth session for this org.

    Runs probes in parallel via ``asyncio.gather(...,
    return_exceptions=True)``, aggregates outcomes, and logs a
    single summary line. The "when to run" concern (sleep / tick
    cadence) stays in the caller (``app.py``'s lifespan), so the
    multi-org loop can iterate every org's one-pass without
    duplicating the aggregation logic.

    Returns the ``{healthy, transient, torn_down, errors}`` counts
    so the caller can log cross-org totals or feed metrics.
    """
    from mcpolis.domain.model.policy import AuthMode

    upstream_by_id = {u.id: u for u in upstreams}
    oauth_upstream_ids = {
        u.id for u in upstreams
        if u.auth.mode in (AuthMode.admin_oauth, AuthMode.per_user_oauth)
    }

    targets = client_manager.iter_live_oauth_sessions(oauth_upstream_ids)
    if not targets:
        return {"healthy": 0, "transient": 0, "torn_down": 0, "errors": 0}

    logger.info(
        "upstream.health.probe.periodic.started",
        org_id=org_id,
        session_count=len(targets),
    )
    tasks: list[Coroutine[object, object, ProbeOutcome]] = []
    for upstream_id, user_id, session in targets:
        upstream = upstream_by_id.get(upstream_id)
        if upstream is None:
            continue
        tasks.append(
            probe_upstream_liveness(
                org_id, upstream, user_id, session,
                client_manager, connection_store, server_url,
            )
        )
    if not tasks:
        return {"healthy": 0, "transient": 0, "torn_down": 0, "errors": 0}

    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = _summarize_probe_outcomes(results)
    logger.info(
        "upstream.health.probe.periodic.completed",
        org_id=org_id,
        healthy=counts["healthy"],
        torn_down=counts["torn_down"],
        transient=counts["transient"],
        errors=counts["errors"],
    )
    return counts


def _summarize_probe_outcomes(
    results: list[ProbeOutcome | BaseException],
) -> dict[str, int]:
    """Aggregate probe outcomes into the four buckets the summary log
    surfaces. A ``BaseException`` return (from ``gather(...,
    return_exceptions=True)``) means the probe itself threw — counted
    separately from ``torn_down`` so an alert on "probe crashed" can
    distinguish the operator-fixable case from a dead session."""
    counts = {"healthy": 0, "transient": 0, "torn_down": 0, "errors": 0}
    for result in results:
        if isinstance(result, ProbeOutcome):
            counts[result.value] += 1
        else:
            counts["errors"] += 1
    return counts
