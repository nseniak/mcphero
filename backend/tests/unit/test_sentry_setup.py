"""``_before_send`` identity enrichment: prefer the real user/org over
the ``anonymous`` / ``default`` sentinels, reading from whichever source
(gateway ContextVars or structlog bound contextvars) carries it.

Regression anchor: MCPOLIS-BACKEND-P, a background ``asyncio.create_task``
tool-registry refresh ``TimeoutError`` that carried the real ``user_id``
in its structlog payload but reported ``email: anonymous`` to Sentry
because the hook only read the (unset) ``current_user_id`` ContextVar.
"""
from __future__ import annotations

from typing import cast

import structlog
from sentry_sdk.types import Event, Hint

from mcpolis.adapters.observability.sentry_setup import (
    _make_before_send,
    _org_sentinels,
)
from mcpolis.entrypoints.config import Mode
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_org_id,
    current_user_id,
)


def make_event(**fields: object) -> Event:
    """Build an Event-shaped dict for the before_send hook."""
    return cast(Event, dict(fields))


def bind_identity(
    *,
    ctx_user: str,
    ctx_org: str,
    bound_user: str | None = None,
    bound_org: str | None = None,
) -> None:
    """Set both identity sources to a known state for one test.

    ``ctx_*`` populate the gateway ContextVars (dashboard-cookie path);
    ``bound_*`` populate structlog's bound contextvars (OAuth/MCP +
    background-task path). Cleared first so prior tests don't leak in.
    """
    structlog.contextvars.clear_contextvars()
    current_user_id.set(ctx_user)
    current_org_id.set(ctx_org)
    binds: dict[str, str] = {}
    if bound_user is not None:
        binds["user_id"] = bound_user
    if bound_org is not None:
        binds["org_id"] = bound_org
    if binds:
        structlog.contextvars.bind_contextvars(**binds)


def run_before_send(event: Event, mode: Mode = "cloud") -> Event:
    before_send = _make_before_send(_org_sentinels(mode))
    result = before_send(event, cast(Hint, {}))
    assert result is not None
    return result


def email_of(event: Event) -> object:
    """Sentry ``user.email``, or None when unset — via ``.get()`` so the
    optional-TypedDict-key checker stays happy."""
    user = event.get("user") or {}
    return user.get("email")


def org_tag_of(event: Event) -> object:
    tags = cast("dict[str, object]", event.get("tags") or {})
    return tags.get("org_id")


def test_before_send_prefers_bound_user_over_anonymous_contextvar() -> None:
    # The MCPOLIS-BACKEND-P shape: ContextVar is the unset "anonymous"
    # default, but structlog carries the real OAuth/MCP user.
    bind_identity(
        ctx_user="anonymous", ctx_org="default", bound_user="real@example.com"
    )
    result = run_before_send(make_event())
    assert email_of(result) == "real@example.com"


def test_before_send_omits_email_when_only_anonymous() -> None:
    bind_identity(ctx_user="anonymous", ctx_org="default")
    result = run_before_send(make_event())
    assert result.get("user") is None


def test_before_send_uses_dashboard_contextvar_user() -> None:
    bind_identity(ctx_user="dash@example.com", ctx_org="default")
    result = run_before_send(make_event())
    assert email_of(result) == "dash@example.com"


def test_before_send_does_not_override_existing_email() -> None:
    bind_identity(ctx_user="ctx@example.com", ctx_org="default")
    result = run_before_send(make_event(user={"email": "preexisting@example.com"}))
    assert email_of(result) == "preexisting@example.com"


def test_before_send_suppresses_default_org_tag_in_cloud() -> None:
    bind_identity(ctx_user="anonymous", ctx_org="default")
    result = run_before_send(make_event(), mode="cloud")
    assert org_tag_of(result) is None


def test_before_send_suppresses_multi_org_sentinel_tag() -> None:
    bind_identity(ctx_user="anonymous", ctx_org="__multi__")
    result = run_before_send(make_event(), mode="cloud")
    assert org_tag_of(result) is None


def test_before_send_tags_real_org_from_bound_context() -> None:
    bind_identity(
        ctx_user="anonymous", ctx_org="default", bound_org="9f535c970ca949c8"
    )
    result = run_before_send(make_event(), mode="cloud")
    assert org_tag_of(result) == "9f535c970ca949c8"


def test_before_send_tags_default_org_in_standalone() -> None:
    # In standalone "default" is the one real org, not a sentinel.
    bind_identity(ctx_user="anonymous", ctx_org="default")
    result = run_before_send(make_event(), mode="standalone")
    assert org_tag_of(result) == "default"


def test_before_send_scrubs_auth_headers() -> None:
    bind_identity(ctx_user="anonymous", ctx_org="default")
    event = make_event(
        request={
            "headers": {
                "Authorization": "Bearer secret",
                "Cookie": "session=abc",
                "User-Agent": "ua",
            }
        }
    )
    result = run_before_send(event)
    request = result.get("request") or {}
    headers = cast("dict[str, str]", request.get("headers") or {})
    assert headers["Authorization"] == "[Filtered]"
    assert headers["Cookie"] == "[Filtered]"
    assert headers["User-Agent"] == "ua"
