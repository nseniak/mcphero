"""Request rate limits: what each surface charges, and against whom.

Four surfaces are limited, each over a sliding one-minute window:

* **Gateway tool calls** — a call the org's policy allows is charged to
  two buckets at once: the caller (a user or a service token) inside
  the org, and the whole org. The numbers come from the org's plan
  (``PlanLimits`` in :mod:`plan_policy`). Only ``tools/call`` counts;
  lists, pings and streams don't. A refused ``tools/call``,
  ``resources/read`` or ``prompts/get`` (denied by policy, naming an org
  the caller isn't in, an unknown tool) is charged to a separate
  per-caller bucket with the lowest plan's per-caller limit and never
  to an org: on ``/mcp/{slug}`` membership is enforced by policy, so
  the org bucket must only ever count calls the org would run, and a
  refusal must not reveal the org's plan.
* **Admin MCP tool calls** — per user.
* **Dashboard API** (``/api/*``) — per signed-in user, per client IP
  when anonymous.
* **Sign-in endpoints** — per client IP, one bucket per
  :class:`SignInGroup`, so a browser stuck posting error reports can't
  close token refresh for every MCP client behind the same IP.

The plan-independent numbers come from ``Settings``
(``RequestRateLimits``). The limits are runaway ceilings, sized at
roughly five times the busiest real traffic; they are not the Terms §3
fair-use boundary and are deliberately not published in the user docs.

A refusal is reported (log line + analytics event) at most once per
bucket per window, so a client hammering a closed bucket can't flood
the logs or the analytics bill.
"""
from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import structlog

from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports.rate_limiter import RateLimitBucket, RateLimiter
from mcpolis.domain.services.emit_throttle import EmitThrottle
from mcpolis.domain.services.plan_policy import FREE, limits_for

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

WINDOW_SECONDS = 60.0

# How long an org's plan is reused before it is read again. Keeps the
# org lookup off the tool-call path; an upgrade applies within this.
PLAN_CACHE_SECONDS = 30.0

# Refused calls are held to the lowest plan's per-caller limit, so the
# limit itself can't tell an outsider which plan the org is on.
DENIED_CALLS_PER_MIN = FREE.tool_calls_per_min_per_caller


class RateLimitSurface(str, Enum):
    tool_call = "tool_call"
    admin_mcp = "admin_mcp"
    dashboard = "dashboard"
    sign_in = "sign_in"


class SignInGroup(str, Enum):
    """Sign-in endpoints that share a per-IP bucket."""

    # Gateway / Admin MCP OAuth: authorize, token (incl. refresh),
    # register, revoke, Google callback.
    mcp_oauth = "mcp_oauth"
    # Dashboard sign-in, dev-stub picker, test token mint, upstream
    # OAuth callback.
    dashboard_sign_in = "dashboard_sign_in"
    # The public org lookup behind invite links.
    org_lookup = "org_lookup"
    # Browser error reports (posted automatically by the dashboard).
    client_errors = "client_errors"


@dataclass(frozen=True)
class RequestRateLimits:
    """Plan-independent limits, per minute. Read from ``Settings``."""

    enabled: bool = True
    sign_in_per_min: int = 30
    dashboard_per_min: int = 300
    admin_mcp_per_min: int = 60


@dataclass(frozen=True)
class RateLimitRefusal:
    """A refused request, ready to show to the caller.

    ``message`` is complete and human-readable (it already names the
    wait); ``retry_after_seconds`` is the same wait as a whole number of
    seconds, at least 1, for ``Retry-After`` headers.
    """

    surface: RateLimitSurface
    gate: str
    retry_after_seconds: int
    message: str


PlanResolver = Callable[[str], Awaitable[PlanName]]
# ``(distinct_id, event, properties)`` — matches ``track_async`` on the
# analytics client, injected so the domain layer doesn't import it.
AnalyticsTrack = Callable[[str, str, dict[str, object]], None]


class RateLimitReporter:
    """Logs and tracks refusals, at most once per bucket per window.

    The first refusal of a bucket in a window emits one
    ``rate_limit.exceeded`` log line (and an analytics event when the
    caller is known); further refusals of that bucket inside the window
    are only counted, and the count rides on the bucket's next line. A
    bucket that goes quiet with refusals still counted gets one closing
    line at the next sweep, so totals in the logs stay exact.
    """

    def __init__(
        self,
        *,
        track: AnalyticsTrack | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._track = track
        self._throttle = EmitThrottle(WINDOW_SECONDS, now=now)
        # Log fields of each bucket's last reported refusal, for the
        # closing line of a bucket that goes quiet.
        self._fields: dict[str, dict[str, object]] = {}

    def report(
        self,
        refusal: RateLimitRefusal,
        bucket: RateLimitBucket,
        *,
        actor: str | None,
        org_id: str | None,
        client_ip: str | None = None,
        plan: PlanName | None = None,
    ) -> None:
        swallowed = self._throttle.attempt(bucket.key)
        self._flush_quiet_buckets()
        if swallowed is None:
            return
        fields: dict[str, object] = {
            "surface": refusal.surface.value,
            "gate": refusal.gate,
            "limit": bucket.limit,
            "retry_after_seconds": refusal.retry_after_seconds,
            "actor": actor,
            "org_id": org_id,
            "client_ip": client_ip,
            "plan": plan.value if plan is not None else None,
        }
        self._fields[bucket.key] = fields
        logger.info(
            "rate_limit.exceeded", **fields, refused_since_last_report=swallowed,
        )
        if self._track is None or actor is None:
            # Anonymous (per-IP) refusals have no person to attach an
            # analytics event to; the log line above is their record.
            return
        self._track(
            _caller_identity(actor, org_id),
            "rate_limit_hit",
            {
                "gate": refusal.gate,
                "limit": bucket.limit,
                "source": refusal.surface.value,
                "org_id": org_id,
                "plan": plan.value if plan is not None else None,
            },
        )

    def _flush_quiet_buckets(self) -> None:
        # Every quiet bucket's fields go, not only those with a count to
        # flush: a bucket refused once (the usual case for a per-IP
        # sign-in bucket) would otherwise be held for the life of the
        # process, one entry per client IP ever refused.
        for key, swallowed in self._throttle.drain_quiet():
            fields = self._fields.pop(key, {})
            if not swallowed:
                continue
            logger.info(
                "rate_limit.exceeded",
                **fields,
                refused_since_last_report=swallowed,
                closing=True,
            )


def _caller_identity(actor: str, org_id: str | None) -> str:
    """Who the caller is, across orgs. Service-token labels are unique
    per org only: qualify them with their org, so two orgs' tokens with
    the same label never share an analytics identity or a refused-call
    bucket. People are the same person in every org."""
    if actor.startswith("svc:") and org_id:
        return f"{actor}@{org_id}"
    return actor


class RateLimitService:
    """Decides whether a request may proceed, per surface.

    Every ``admit_*`` method returns ``None`` to admit the request, or a
    ``RateLimitRefusal`` to refuse it. Failures of the limiting
    machinery itself (Redis down, plan lookup failing) admit: rate
    limiting must never be the reason the gateway stops working.
    """

    def __init__(
        self,
        limiter: RateLimiter,
        limits: RequestRateLimits,
        *,
        plan_for_org: PlanResolver,
        reporter: RateLimitReporter | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limiter = limiter
        self._limits = limits
        self._plan_for_org = plan_for_org
        self._reporter = reporter or RateLimitReporter()
        self._now = now
        self._plans: dict[str, tuple[PlanName, float]] = {}

    async def admit_tool_call(
        self, *, org_id: str, caller: str,
    ) -> RateLimitRefusal | None:
        """Charge one allowed gateway tool call to the caller and the org."""
        if not self._limits.enabled:
            return None
        try:
            plan = await self._plan(org_id)
        except Exception:
            logger.exception("rate_limit.plan_lookup.failed_open", org_id=org_id)
            return None
        plan_limits = limits_for(plan)
        caller_bucket = RateLimitBucket(
            key=f"tool_call:caller:{org_id}:{caller}",
            limit=plan_limits.tool_calls_per_min_per_caller,
            window_seconds=WINDOW_SECONDS,
        )
        org_bucket = RateLimitBucket(
            key=f"tool_call:org:{org_id}",
            limit=plan_limits.tool_calls_per_min_per_org,
            window_seconds=WINDOW_SECONDS,
        )
        result = await self._limiter.check(caller_bucket, org_bucket)
        if result.allowed:
            return None
        exceeded = result.exceeded or caller_bucket
        wait = _whole_seconds(result.retry_after)
        upgrade_hint = (
            " The Team plan has higher limits." if plan == PlanName.free else ""
        )
        if exceeded == org_bucket:
            refusal = RateLimitRefusal(
                surface=RateLimitSurface.tool_call,
                gate="tool_calls_per_min_per_org",
                retry_after_seconds=wait,
                message=(
                    "Rate limit reached: your organization has made too "
                    "many tool calls in the last minute. "
                    f"Try again in {_seconds(wait)}.{upgrade_hint}"
                ),
            )
        else:
            refusal = RateLimitRefusal(
                surface=RateLimitSurface.tool_call,
                gate="tool_calls_per_min_per_caller",
                retry_after_seconds=wait,
                message=(
                    "Rate limit reached: you have made too many tool calls "
                    "in this organization in the last minute. "
                    f"Try again in {_seconds(wait)}.{upgrade_hint}"
                ),
            )
        self._reporter.report(
            refusal, exceeded, actor=caller, org_id=org_id, plan=plan,
        )
        return refusal

    async def admit_denied_tool_call(
        self, *, caller: str, org_id: str | None,
    ) -> RateLimitRefusal | None:
        """Charge one refused gateway request to the caller only.

        Covers every refusal path of tools/call, resources/read and
        prompts/get: denied by policy, naming an org the caller isn't a
        member of, an unknown tool, prompt or resource. No org bucket and
        no plan lookup: whoever the caller is, these requests can neither
        spend nor reveal anything about an org. ``org_id`` (when known)
        labels the log line, and keeps a service token's bucket apart
        from another org's token with the same label.
        """
        if not self._limits.enabled:
            return None
        bucket = RateLimitBucket(
            key=f"tool_call:denied:{_caller_identity(caller, org_id)}",
            limit=DENIED_CALLS_PER_MIN,
            window_seconds=WINDOW_SECONDS,
        )
        result = await self._limiter.check(bucket)
        if result.allowed:
            return None
        wait = _whole_seconds(result.retry_after)
        refusal = RateLimitRefusal(
            surface=RateLimitSurface.tool_call,
            gate="denied_tool_calls_per_min",
            retry_after_seconds=wait,
            message=(
                "Rate limit reached: you have made too many requests that "
                f"were refused in the last minute. Try again in {_seconds(wait)}."
            ),
        )
        self._reporter.report(refusal, bucket, actor=caller, org_id=org_id)
        return refusal

    async def admit_admin_mcp_call(
        self, *, user: str, org_id: str | None,
    ) -> RateLimitRefusal | None:
        """Charge one Admin MCP tool call to the user."""
        if not self._limits.enabled:
            return None
        bucket = RateLimitBucket(
            key=f"admin_mcp:user:{user}",
            limit=self._limits.admin_mcp_per_min,
            window_seconds=WINDOW_SECONDS,
        )
        result = await self._limiter.check(bucket)
        if result.allowed:
            return None
        wait = _whole_seconds(result.retry_after)
        refusal = RateLimitRefusal(
            surface=RateLimitSurface.admin_mcp,
            gate="admin_mcp_calls_per_min",
            retry_after_seconds=wait,
            message=(
                "Rate limit reached: you have made too many Admin MCP calls "
                f"in the last minute. Try again in {_seconds(wait)}."
            ),
        )
        self._reporter.report(refusal, bucket, actor=user, org_id=org_id)
        return refusal

    async def admit_dashboard_request(
        self, *, user: str | None, client_ip: str,
    ) -> RateLimitRefusal | None:
        """Charge one dashboard API request to the signed-in user, or to
        the client IP when nobody is signed in."""
        if not self._limits.enabled:
            return None
        key = f"dashboard:user:{user}" if user else f"dashboard:ip:{client_ip}"
        bucket = RateLimitBucket(
            key=key,
            limit=self._limits.dashboard_per_min,
            window_seconds=WINDOW_SECONDS,
        )
        result = await self._limiter.check(bucket)
        if result.allowed:
            return None
        wait = _whole_seconds(result.retry_after)
        refusal = RateLimitRefusal(
            surface=RateLimitSurface.dashboard,
            gate="dashboard_requests_per_min",
            retry_after_seconds=wait,
            message=f"Too many requests. Try again in {_seconds(wait)}.",
        )
        self._reporter.report(
            refusal, bucket, actor=user, org_id=None, client_ip=client_ip,
        )
        return refusal

    async def admit_sign_in_request(
        self, *, client_ip: str, group: SignInGroup,
    ) -> RateLimitRefusal | None:
        """Charge one sign-in request to the client IP, in its group."""
        if not self._limits.enabled:
            return None
        bucket = RateLimitBucket(
            key=f"sign_in:{group.value}:{client_ip}",
            limit=self._limits.sign_in_per_min,
            window_seconds=WINDOW_SECONDS,
        )
        result = await self._limiter.check(bucket)
        if result.allowed:
            return None
        wait = _whole_seconds(result.retry_after)
        what = {
            SignInGroup.mcp_oauth: "sign-in requests",
            SignInGroup.dashboard_sign_in: "sign-in requests",
            SignInGroup.org_lookup: "requests",
            SignInGroup.client_errors: "error reports",
        }[group]
        refusal = RateLimitRefusal(
            surface=RateLimitSurface.sign_in,
            gate=f"{group.value}_requests_per_min",
            retry_after_seconds=wait,
            message=(
                f"Too many {what} from your network. "
                f"Try again in {_seconds(wait)}."
            ),
        )
        self._reporter.report(
            refusal, bucket, actor=None, org_id=None, client_ip=client_ip,
        )
        return refusal

    async def _plan(self, org_id: str) -> PlanName:
        now = self._now()
        cached = self._plans.get(org_id)
        if cached is not None and now - cached[1] < PLAN_CACHE_SECONDS:
            return cached[0]
        plan = await self._plan_for_org(org_id)
        self._plans[org_id] = (plan, now)
        return plan


def _whole_seconds(retry_after: float | None) -> int:
    """Round a wait up to whole seconds, never below 1.

    Rounding up (not to nearest) so a client that waits exactly the
    advertised time finds the bucket open.
    """
    if retry_after is None:
        return 1
    return max(1, math.ceil(retry_after))


def _seconds(n: int) -> str:
    return "1 second" if n == 1 else f"{n} seconds"
