"""RateLimitService: who each surface charges, and what a refusal says.

Driven through the real ``InProcessRateLimiter`` with a fake clock, so
every assertion is deterministic. The Redis adapter has the same
contract and is covered in ``test_rate_limiter.py``.
"""
from __future__ import annotations

import structlog
from structlog.typing import EventDict

from mcpolis.adapters.rate_limiter_inprocess import InProcessRateLimiter
from mcpolis.domain.model.service_token import service_identity
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports.rate_limiter import RateLimitBucket
from mcpolis.domain.services.plan_policy import FREE, TEAM
from mcpolis.domain.services.rate_limit_service import (
    DENIED_CALLS_PER_MIN,
    PLAN_CACHE_SECONDS,
    WINDOW_SECONDS,
    RateLimitRefusal,
    RateLimitReporter,
    RateLimitService,
    RateLimitSurface,
    RequestRateLimits,
    SignInGroup,
)
from tests.unit.test_rate_limiter import FakeClock


class RecordingTrack:
    """Stands in for ``AnalyticsClient.track_async``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self, distinct_id: str, event: str, properties: dict[str, object],
    ) -> None:
        self.events.append((distinct_id, event, properties))


class PlanBook:
    """Org plans for the service under test; counts lookups. Unknown
    orgs are Free."""

    def __init__(self, plans: dict[str, PlanName] | None = None) -> None:
        self.plans = dict(plans or {})
        self.lookups = 0
        self.fail = False

    async def plan_for_org(self, org_id: str) -> PlanName:
        self.lookups += 1
        if self.fail:
            raise RuntimeError("org store unreachable")
        return self.plans.get(org_id, PlanName.free)


def make_service(
    *,
    plans: dict[str, PlanName] | None = None,
    limits: RequestRateLimits | None = None,
    clock: FakeClock | None = None,
    track: RecordingTrack | None = None,
    plan_lookup_fails: bool = False,
    plan_book: PlanBook | None = None,
) -> RateLimitService:
    """A service over a fresh in-process limiter."""
    the_clock = clock or FakeClock()
    book = plan_book or PlanBook(plans)
    book.fail = book.fail or plan_lookup_fails
    return RateLimitService(
        InProcessRateLimiter(now=the_clock),
        limits or RequestRateLimits(),
        plan_for_org=book.plan_for_org,
        reporter=RateLimitReporter(track=track, now=the_clock),
        now=the_clock,
    )


async def make_tool_calls(
    service: RateLimitService, count: int, *, org_id: str = "org-1",
    caller: str = "alice@example.com",
) -> list[RateLimitRefusal | None]:
    return [
        await service.admit_tool_call(org_id=org_id, caller=caller)
        for _ in range(count)
    ]


# ── Gateway tool calls ────────────────────────────────────────────────


async def test_free_caller_is_refused_after_the_plan_limit() -> None:
    service = make_service()
    admitted = await make_tool_calls(service, FREE.tool_calls_per_min_per_caller)
    assert admitted == [None] * FREE.tool_calls_per_min_per_caller

    refusal = await service.admit_tool_call(org_id="org-1", caller="alice@example.com")

    assert refusal is not None
    assert refusal.gate == "tool_calls_per_min_per_caller"
    assert refusal.message == (
        "Rate limit reached: you have made too many tool calls in this "
        "organization in the last minute. Try again in 60 seconds. "
        "The Team plan has higher limits."
    )


async def test_team_caller_gets_the_team_limit_and_no_upgrade_hint() -> None:
    service = make_service(plans={"org-1": PlanName.team})
    admitted = await make_tool_calls(service, TEAM.tool_calls_per_min_per_caller)
    assert admitted == [None] * TEAM.tool_calls_per_min_per_caller

    refusal = await service.admit_tool_call(org_id="org-1", caller="alice@example.com")

    assert refusal is not None
    assert "Team plan" not in refusal.message


async def test_org_limit_spans_all_callers_of_the_org() -> None:
    service = make_service()
    per_caller = FREE.tool_calls_per_min_per_caller
    callers = FREE.tool_calls_per_min_per_org // per_caller
    for i in range(callers):
        admitted = await make_tool_calls(service, per_caller, caller=f"user{i}@example.com")
        assert admitted == [None] * per_caller

    refusal = await service.admit_tool_call(org_id="org-1", caller="fresh@example.com")

    assert refusal is not None
    assert refusal.gate == "tool_calls_per_min_per_org"
    assert refusal.message.startswith(
        "Rate limit reached: your organization has made too many tool calls",
    )


async def test_a_refused_caller_does_not_spend_its_teammates_quota() -> None:
    service = make_service()
    per_caller = FREE.tool_calls_per_min_per_caller
    await make_tool_calls(service, per_caller, caller="runaway@example.com")
    refused = await make_tool_calls(service, 500, caller="runaway@example.com")
    assert all(r is not None for r in refused)

    # The org has spent only the runaway's admitted calls, so a
    # teammate still gets a full caller quota (60 + 60 = the org's 120).
    teammate = await make_tool_calls(service, per_caller, caller="bob@example.com")

    assert teammate == [None] * per_caller


async def make_denied_calls(
    service: RateLimitService, count: int, *, caller: str = "outsider@example.com",
) -> list[RateLimitRefusal | None]:
    return [
        await service.admit_denied_tool_call(caller=caller, org_id="org-1")
        for _ in range(count)
    ]


async def test_refused_calls_have_their_own_bucket_and_never_spend_an_org() -> None:
    service = make_service()
    per_caller = FREE.tool_calls_per_min_per_caller
    assert await make_denied_calls(service, per_caller) == [None] * per_caller

    refused = await service.admit_denied_tool_call(
        caller="outsider@example.com", org_id="org-1",
    )

    assert refused is not None
    assert refused.gate == "denied_tool_calls_per_min"
    assert refused.message == (
        "Rate limit reached: you have made too many requests that were "
        "refused in the last minute. Try again in 60 seconds."
    )
    # The org bucket is untouched: two members still share its full quota.
    for member in ("a@example.com", "b@example.com"):
        admitted = await make_tool_calls(service, per_caller, caller=member)
        assert admitted == [None] * per_caller


async def test_refused_calls_reveal_nothing_about_the_orgs_plan() -> None:
    """Same limit and same words whatever the org's plan, and no plan
    lookup at all: an outsider learns nothing about the org."""
    book = PlanBook({"org-1": PlanName.team})
    service = make_service(plan_book=book)
    admitted = await make_denied_calls(service, FREE.tool_calls_per_min_per_caller)

    refused = await service.admit_denied_tool_call(
        caller="outsider@example.com", org_id="org-1",
    )

    assert admitted == [None] * FREE.tool_calls_per_min_per_caller
    assert refused is not None
    assert "plan" not in refused.message.lower()
    assert book.lookups == 0


async def test_a_service_tokens_refused_calls_are_counted_in_its_own_org() -> None:
    """Token labels are unique per org only. Another org's token with the
    same label must not spend this token's refused-call budget: its next
    refusal would become a rate-limit refusal, which writes no ``denied``
    audit row."""
    service = make_service()
    bot = service_identity("ci-bot")
    for _ in range(DENIED_CALLS_PER_MIN):
        await service.admit_denied_tool_call(caller=bot, org_id="org-a")

    other_orgs_bot = await service.admit_denied_tool_call(caller=bot, org_id="org-b")
    same_bot_again = await service.admit_denied_tool_call(caller=bot, org_id="org-a")

    assert other_orgs_bot is None
    assert same_bot_again is not None


async def test_a_persons_refused_calls_share_one_budget_across_orgs() -> None:
    """A person is the same caller in every org: naming more orgs must not
    buy an outsider more refused calls."""
    service = make_service()
    for i in range(DENIED_CALLS_PER_MIN):
        await service.admit_denied_tool_call(
            caller="outsider@example.com", org_id=f"org-{i}",
        )

    refusal = await service.admit_denied_tool_call(
        caller="outsider@example.com", org_id="one-more-org",
    )

    assert refusal is not None


async def test_a_members_refused_calls_leave_its_tool_call_quota_alone() -> None:
    service = make_service()
    await make_denied_calls(
        service, FREE.tool_calls_per_min_per_caller + 5, caller="alice@example.com",
    )

    admitted = await make_tool_calls(service, FREE.tool_calls_per_min_per_caller)

    assert admitted == [None] * FREE.tool_calls_per_min_per_caller


async def test_plan_is_read_once_per_cache_period() -> None:
    clock = FakeClock()
    book = PlanBook({"org-1": PlanName.free})
    service = make_service(clock=clock, plan_book=book)
    await make_tool_calls(service, 10)
    assert book.lookups == 1

    # An upgrade applies once the cached plan expires.
    book.plans["org-1"] = PlanName.team
    clock.advance(PLAN_CACHE_SECONDS)
    await make_tool_calls(service, FREE.tool_calls_per_min_per_caller - 10)
    over_free = await service.admit_tool_call(org_id="org-1", caller="alice@example.com")

    assert book.lookups == 2
    assert over_free is None  # 61st call, admitted under the Team limit


async def test_orgs_are_counted_separately() -> None:
    service = make_service()
    await make_tool_calls(service, FREE.tool_calls_per_min_per_caller, org_id="org-1")

    other_org = await service.admit_tool_call(org_id="org-2", caller="alice@example.com")

    assert other_org is None


async def test_refusal_wait_counts_down_until_the_oldest_call_ages_out() -> None:
    clock = FakeClock()
    service = make_service(clock=clock)
    await make_tool_calls(service, FREE.tool_calls_per_min_per_caller)

    clock.advance(45.5)
    refusal = await service.admit_tool_call(org_id="org-1", caller="alice@example.com")
    assert refusal is not None
    # 14.5 s left, rounded up so a client waiting exactly that long finds room.
    assert refusal.retry_after_seconds == 15
    assert "Try again in 15 seconds." in refusal.message

    clock.advance(14.5)
    assert await service.admit_tool_call(org_id="org-1", caller="alice@example.com") is None


async def test_plan_lookup_failure_admits_the_call() -> None:
    """Rate limiting must never be the reason the gateway stops: if the
    org's plan can't be read, the call goes through unlimited."""
    service = make_service(plan_lookup_fails=True)

    results = await make_tool_calls(service, FREE.tool_calls_per_min_per_caller + 10)

    assert results == [None] * (FREE.tool_calls_per_min_per_caller + 10)


async def test_switched_off_admits_everything() -> None:
    service = make_service(limits=RequestRateLimits(enabled=False))

    tool_calls = await make_tool_calls(service, 1_000)
    sign_ins = [
        await service.admit_sign_in_request(client_ip="10.0.0.1", group=SignInGroup.mcp_oauth)
        for _ in range(1_000)
    ]

    assert tool_calls == [None] * 1_000
    assert sign_ins == [None] * 1_000


# ── Admin MCP, dashboard, sign-in ─────────────────────────────────────


async def test_admin_mcp_calls_are_limited_per_user() -> None:
    service = make_service(limits=RequestRateLimits(admin_mcp_per_min=3))
    for _ in range(3):
        assert await service.admit_admin_mcp_call(user="admin@example.com", org_id="org-1") is None

    refusal = await service.admit_admin_mcp_call(user="admin@example.com", org_id="org-1")
    other_user = await service.admit_admin_mcp_call(user="other@example.com", org_id="org-1")

    assert refusal is not None
    assert refusal.message.startswith("Rate limit reached: you have made too many Admin MCP calls")
    assert other_user is None


async def test_dashboard_counts_signed_in_users_apart_from_their_ip() -> None:
    service = make_service(limits=RequestRateLimits(dashboard_per_min=2))
    office_ip = "10.0.0.1"
    admit = service.admit_dashboard_request
    for _ in range(2):
        assert await admit(user="a@example.com", client_ip=office_ip) is None

    refused = await admit(user="a@example.com", client_ip=office_ip)
    teammate_same_office = await admit(user="b@example.com", client_ip=office_ip)
    anonymous_same_office = await admit(user=None, client_ip=office_ip)

    assert refused is not None
    assert refused.message == "Too many requests. Try again in 60 seconds."
    assert teammate_same_office is None
    assert anonymous_same_office is None


async def test_dashboard_counts_anonymous_requests_per_ip() -> None:
    service = make_service(limits=RequestRateLimits(dashboard_per_min=2))
    for _ in range(2):
        assert await service.admit_dashboard_request(user=None, client_ip="10.0.0.1") is None

    refused = await service.admit_dashboard_request(user=None, client_ip="10.0.0.1")
    other_ip = await service.admit_dashboard_request(user=None, client_ip="10.0.0.2")

    assert refused is not None
    assert other_ip is None


async def test_sign_in_is_limited_per_ip() -> None:
    service = make_service(limits=RequestRateLimits(sign_in_per_min=3))
    oauth = SignInGroup.mcp_oauth
    for _ in range(3):
        assert await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth) is None

    refused = await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)
    other_ip = await service.admit_sign_in_request(client_ip="10.0.0.2", group=oauth)

    assert refused is not None
    assert refused.gate == "mcp_oauth_requests_per_min"
    assert refused.message == (
        "Too many sign-in requests from your network. Try again in 60 seconds."
    )
    assert other_ip is None


async def test_sign_in_groups_are_counted_separately() -> None:
    """A browser stuck posting error reports must not close MCP token
    refresh for everyone behind the same IP."""
    service = make_service(limits=RequestRateLimits(sign_in_per_min=3))
    for _ in range(10):
        await service.admit_sign_in_request(client_ip="10.0.0.1", group=SignInGroup.client_errors)

    token_refresh = await service.admit_sign_in_request(
        client_ip="10.0.0.1", group=SignInGroup.mcp_oauth,
    )
    report = await service.admit_sign_in_request(
        client_ip="10.0.0.1", group=SignInGroup.client_errors,
    )

    assert token_refresh is None
    assert report is not None
    assert report.message.startswith("Too many error reports from your network.")


async def test_one_second_wait_is_singular() -> None:
    clock = FakeClock()
    service = make_service(limits=RequestRateLimits(sign_in_per_min=1), clock=clock)
    oauth = SignInGroup.mcp_oauth
    await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)
    clock.advance(59.5)

    refused = await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)

    assert refused is not None
    assert refused.retry_after_seconds == 1
    assert refused.message.endswith("Try again in 1 second.")


# ── Reporting ─────────────────────────────────────────────────────────


def exceeded_lines(logs: list[EventDict]) -> list[EventDict]:
    return [line for line in logs if line["event"] == "rate_limit.exceeded"]


async def test_refusals_are_reported_once_per_bucket_per_window() -> None:
    clock = FakeClock()
    track = RecordingTrack()
    service = make_service(clock=clock, track=track)
    await make_tool_calls(service, FREE.tool_calls_per_min_per_caller)

    with structlog.testing.capture_logs() as logs:
        await make_tool_calls(service, 50)
        first = exceeded_lines(logs)
        clock.advance(30)
        # Still refused (the first calls haven't aged out) and still
        # inside the reporting window: counted, not logged.
        await make_tool_calls(service, 5)
        assert exceeded_lines(logs) == first
        clock.advance(30)
        # A full window later the first calls have aged out: the caller
        # fills the bucket again, and the next refusal is reported.
        await make_tool_calls(service, FREE.tool_calls_per_min_per_caller)
        await make_tool_calls(service, 1)
        second = exceeded_lines(logs)[1:]

    assert len(first) == 1
    assert first[0]["gate"] == "tool_calls_per_min_per_caller"
    assert first[0]["actor"] == "alice@example.com"
    assert first[0]["org_id"] == "org-1"
    assert first[0]["plan"] == "free"
    assert len(second) == 1
    # The 49 + 5 refusals swallowed since the first line ride on the next.
    assert second[0]["refused_since_last_report"] == 54
    assert [event for _, event, _ in track.events] == ["rate_limit_hit", "rate_limit_hit"]
    distinct_id, _, properties = track.events[0]
    assert distinct_id == "alice@example.com"
    assert properties == {
        "gate": "tool_calls_per_min_per_caller",
        "limit": FREE.tool_calls_per_min_per_caller,
        "source": "tool_call",
        "org_id": "org-1",
        "plan": "free",
    }


async def test_anonymous_refusals_are_logged_but_not_tracked() -> None:
    track = RecordingTrack()
    service = make_service(limits=RequestRateLimits(sign_in_per_min=1), track=track)
    oauth = SignInGroup.mcp_oauth
    await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)

    with structlog.testing.capture_logs() as logs:
        await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)

    lines = exceeded_lines(logs)
    assert len(lines) == 1
    assert lines[0]["client_ip"] == "10.0.0.1"
    assert lines[0]["actor"] is None
    assert track.events == []


async def test_refusals_of_a_bucket_that_goes_quiet_are_still_counted() -> None:
    """50 refusals in one minute log one line; the other 49 must still
    reach the logs (on a closing line) when the bucket goes quiet."""
    clock = FakeClock()
    service = make_service(limits=RequestRateLimits(sign_in_per_min=1), clock=clock)
    oauth = SignInGroup.mcp_oauth
    await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)
    with structlog.testing.capture_logs() as logs:
        for _ in range(50):
            await service.admit_sign_in_request(client_ip="10.0.0.1", group=oauth)
        # Three windows later another bucket's refusal triggers the sweep.
        clock.advance(181)
        await service.admit_sign_in_request(client_ip="10.0.0.2", group=oauth)
        await service.admit_sign_in_request(client_ip="10.0.0.2", group=oauth)

    first_ip = [line for line in exceeded_lines(logs) if line.get("client_ip") == "10.0.0.1"]
    # A line stands for its own refusal plus the ones it carries; a
    # closing line only carries.
    own = sum(1 for line in first_ip if not line.get("closing"))
    carried = sum(int(line["refused_since_last_report"]) for line in first_ip)
    assert own + carried == 50
    assert first_ip[-1]["closing"] is True
    assert first_ip[-1]["refused_since_last_report"] == 49


def make_sign_in_refusal() -> RateLimitRefusal:
    return RateLimitRefusal(
        surface=RateLimitSurface.sign_in,
        gate="mcp_oauth_requests_per_min",
        retry_after_seconds=60,
        message="Too many sign-in requests from your network. Try again in 60 seconds.",
    )


def make_sign_in_bucket(client_ip: str) -> RateLimitBucket:
    return RateLimitBucket(
        key=f"sign_in:mcp_oauth:{client_ip}", limit=30, window_seconds=WINDOW_SECONDS,
    )


def test_quiet_buckets_refused_once_are_forgotten() -> None:
    """A bucket refused once (the usual case for a per-IP sign-in bucket)
    has no swallowed refusals to flush when it goes quiet, and must be
    forgotten all the same: otherwise a caller rotating client IPs grows
    the reporter by one entry per IP for the life of the process. With
    nothing to flush, it gets no closing line either."""
    clock = FakeClock()
    reporter = RateLimitReporter(now=clock)
    for i in range(1000):
        reporter.report(
            make_sign_in_refusal(), make_sign_in_bucket(f"10.0.{i // 250}.{i % 250}"),
            actor=None, org_id=None, client_ip=f"10.0.{i // 250}.{i % 250}",
        )
    clock.advance(3 * WINDOW_SECONDS)

    # Any later refusal runs the sweep.
    with structlog.testing.capture_logs() as logs:
        reporter.report(
            make_sign_in_refusal(), make_sign_in_bucket("192.0.2.1"),
            actor=None, org_id=None, client_ip="192.0.2.1",
        )

    held = reporter._fields  # pyright: ignore[reportPrivateUsage]
    assert list(held) == ["sign_in:mcp_oauth:192.0.2.1"]
    assert [line["client_ip"] for line in exceeded_lines(logs)] == ["192.0.2.1"]


async def test_service_token_analytics_id_names_its_org() -> None:
    """Token labels are unique per org only; two orgs' ``svc:bot`` must
    not merge into one analytics identity."""
    track = RecordingTrack()
    service = make_service(track=track)
    for org in ("org-1", "org-2"):
        await make_tool_calls(
            service, FREE.tool_calls_per_min_per_caller + 1, org_id=org, caller="svc:bot",
        )

    assert [distinct_id for distinct_id, _, _ in track.events] == [
        "svc:bot@org-1", "svc:bot@org-2",
    ]
