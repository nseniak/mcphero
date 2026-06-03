"""Shared plan-gate assertion helpers.

Every code path that creates resources subject to plan limits — the
dashboard routes (``users_admin``, ``upstream_admin``, ``roles``) and
the admin MCP controller — calls into these helpers so the gate
predicates and error messages can never drift between surfaces.

Each helper:

1. Loads ``limits_for(plan)`` from
   :mod:`mcpolis.domain.services.plan_policy` (the single source of
   truth for limits).
2. Returns silently when the relevant cap is ``None`` (Team /
   unlimited).
3. On breach: emits a ``plan_limit_hit`` analytics event with
   ``{gate, current, limit, source, org_id}`` so server-side demand
   data doesn't depend on the client always firing, then raises
   ``PlanLimitExceeded``. The FastAPI exception handler in
   :mod:`mcpolis.entrypoints.app` maps the exception to HTTP 402
   for dashboard callers; admin MCP tool wrappers catch it and
   downgrade to a string error.

The ``source`` kwarg is a surface label like ``"dashboard.add_user"``
or ``"admin_mcp.add_user"``. ``org_id`` and ``actor_email`` feed the
analytics event (``actor_email`` is the distinct_id; ``org_id`` is
on the property bag for analytics filtering).
"""
from __future__ import annotations

from mcpolis.adapters.observability.analytics_client import get_analytics
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.ports.organization_repository import OrganizationRepository
from mcpolis.domain.services.plan_policy import (
    PlanLimitExceeded,
    PlanLimits,
    limits_for,
)


async def resolve_plan(
    org_repo: OrganizationRepository | None, org_id: str,
) -> PlanName:
    """Look up the active org's plan.

    Defaults to ``PlanName.free`` when ``org_repo`` is unavailable or
    the org row is missing — a repo gap can't accidentally widen
    limits. Mirrors the fallback semantic of
    :func:`mcpolis.entrypoints.routes.dashboard._deps.resolve_plan_limits`.
    """
    if org_repo is None:
        return PlanName.free
    org = await org_repo.get_organization(org_id)
    if org is None:
        return PlanName.free
    return org.subscription.plan


async def resolve_plan_limits(
    org_repo: OrganizationRepository | None, org_id: str,
) -> PlanLimits:
    """Look up the active org's plan limits via :func:`resolve_plan`."""
    return limits_for(await resolve_plan(org_repo, org_id))


def _emit_plan_limit_hit(
    *,
    actor_email: str,
    gate: str,
    current: int | str | None,
    limit: int | None,
    source: str,
    org_id: str,
) -> None:
    """Fire the ``plan_limit_hit`` analytics event.

    Properties are ``{gate, current, limit, source, org_id}``. The
    ``current`` field is typed loosely because the sandbox-combo gate
    historically reports ``"{cpu}x{mem}"`` rather than an integer.
    """
    get_analytics().track_async(
        actor_email,
        "plan_limit_hit",
        {
            "gate": gate,
            "current": current,
            "limit": limit,
            "source": source,
            "org_id": org_id,
        },
    )


def assert_seat_capacity(
    plan: PlanName,
    current_count: int,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block the next add-user when ``current_count`` already meets the
    seat cap. No-op on Team (cap is ``None``)."""
    limits = limits_for(plan)
    if limits.max_seats is None:
        return
    if current_count >= limits.max_seats:
        _emit_plan_limit_hit(
            actor_email=actor_email,
            gate="max_seats",
            current=current_count,
            limit=limits.max_seats,
            source=source,
            org_id=org_id,
        )
        raise PlanLimitExceeded(
            "max_seats",
            current_count,
            limits.max_seats,
            f"Free plans are limited to {limits.max_seats} teammates.",
        )


def assert_http_upstream_capacity(
    plan: PlanName,
    current_count: int,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block the next remote HTTP upstream when the cap is reached."""
    limits = limits_for(plan)
    if limits.max_http_upstreams is None:
        return
    if current_count >= limits.max_http_upstreams:
        _emit_plan_limit_hit(
            actor_email=actor_email,
            gate="max_http_upstreams",
            current=current_count,
            limit=limits.max_http_upstreams,
            source=source,
            org_id=org_id,
        )
        raise PlanLimitExceeded(
            "max_http_upstreams",
            current_count,
            limits.max_http_upstreams,
            f"Free plans are limited to {limits.max_http_upstreams} "
            "remote HTTP MCPs.",
        )


def assert_stdio_upstream_capacity(
    plan: PlanName,
    current_count: int,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block the next hosted-stdio upstream when the cap is reached."""
    limits = limits_for(plan)
    if limits.max_stdio_upstreams is None:
        return
    if current_count >= limits.max_stdio_upstreams:
        _emit_plan_limit_hit(
            actor_email=actor_email,
            gate="max_stdio_upstreams",
            current=current_count,
            limit=limits.max_stdio_upstreams,
            source=source,
            org_id=org_id,
        )
        raise PlanLimitExceeded(
            "max_stdio_upstreams",
            current_count,
            limits.max_stdio_upstreams,
            f"Free plans are limited to {limits.max_stdio_upstreams} "
            "hosted stdio MCP.",
        )


def assert_custom_role_capacity(
    plan: PlanName,
    current_count: int,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block the next custom role when the cap is reached.

    ``current_count`` is the count of roles excluding ``is_admin`` and
    ``is_default`` — that classification stays at the call site so the
    helper doesn't need to walk the policy config.
    """
    limits = limits_for(plan)
    if limits.max_custom_roles is None:
        return
    if current_count >= limits.max_custom_roles:
        _emit_plan_limit_hit(
            actor_email=actor_email,
            gate="max_custom_roles",
            current=current_count,
            limit=limits.max_custom_roles,
            source=source,
            org_id=org_id,
        )
        raise PlanLimitExceeded(
            "max_custom_roles",
            current_count,
            limits.max_custom_roles,
            "Free plans don't support custom roles.",
        )


def assert_argument_constraints_allowed(
    plan: PlanName,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block argument-constraint mutations on plans where the feature
    is off."""
    limits = limits_for(plan)
    if limits.allow_argument_constraints:
        return
    _emit_plan_limit_hit(
        actor_email=actor_email,
        gate="allow_argument_constraints",
        current=None,
        limit=None,
        source=source,
        org_id=org_id,
    )
    raise PlanLimitExceeded(
        "allow_argument_constraints",
        None,
        None,
        "Argument checks aren't available on Free plans.",
    )


def assert_sandbox_combo_allowed(
    plan: PlanName,
    cpu_vcpus: float,
    memory_mb: int,
    *,
    source: str,
    org_id: str,
    actor_email: str,
) -> None:
    """Block off-grid sandbox combos for plans with a fixed allow-list.

    No-op when ``allowed_sandbox_combos is None`` (Team / unrestricted).
    Callers must run the provider-grid validator
    (``SandboxService.validate_resources``) BEFORE this helper so
    off-grid values get the more-specific structured 400 with a field
    hint, leaving this helper to surface the plan reason for combos
    the provider does support but the plan does not.
    """
    limits = limits_for(plan)
    if limits.allowed_sandbox_combos is None:
        return
    combo = (int(cpu_vcpus), memory_mb)
    if combo in limits.allowed_sandbox_combos:
        return
    _emit_plan_limit_hit(
        actor_email=actor_email,
        gate="allowed_sandbox_combos",
        current=f"{combo[0]}x{combo[1]}",
        limit=None,
        source=source,
        org_id=org_id,
    )
    raise PlanLimitExceeded(
        "allowed_sandbox_combos",
        None,
        None,
        "This sandbox size isn't available on your plan yet.",
    )
