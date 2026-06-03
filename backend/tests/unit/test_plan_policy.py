"""Plan policy: ``limits_for`` returns the expected dataclasses."""
from __future__ import annotations

from mcpolis.adapters.sandbox_e2b.template_grid import CPU_RAM_PAIRS
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.services.plan_policy import FREE, TEAM, limits_for


def test_limits_for_free() -> None:
    limits = limits_for(PlanName.free)
    assert limits is FREE
    assert limits.max_seats == 3
    assert limits.max_http_upstreams == 5
    assert limits.max_stdio_upstreams == 1
    assert limits.max_custom_roles == 0
    assert limits.allow_argument_constraints is False
    assert limits.audit_retention_days == 30
    # Free is capped to the smallest grid combo (1 vCPU / 1 GB), which
    # is also the model default — so the wizard default, the no-resource
    # create fallback, and the plan allow-list all agree on Free.
    assert limits.allowed_sandbox_combos == ((1, 1024),)


def test_limits_for_team() -> None:
    limits = limits_for(PlanName.team)
    assert limits is TEAM
    assert limits.max_seats is None
    assert limits.max_http_upstreams is None
    assert limits.max_stdio_upstreams is None
    assert limits.max_custom_roles is None
    assert limits.allow_argument_constraints is True
    assert limits.audit_retention_days == 365
    # Team is unrestricted: ``None`` means the sandbox-combo gate is a
    # no-op, so every provider-grid size is selectable. (Previously
    # pinned to ((1, 2048),), which silently capped Team to the one
    # Free size — see the add-wizard 402 investigation.)
    assert limits.allowed_sandbox_combos is None


def test_free_sandbox_combos_exist_on_e2b_grid() -> None:
    # Every plan-enabled combo must be a real E2B template pair, or the
    # capabilities endpoint marks nothing ``enabled`` and the add-wizard
    # seed (``firstEnabledCombo``) has no plan-valid default to pick —
    # the exact failure that surfaced the misleading "larger sizes" 402.
    assert FREE.allowed_sandbox_combos is not None
    assert FREE.allowed_sandbox_combos, "Free must permit at least one combo"
    grid = set(CPU_RAM_PAIRS)
    for pair in FREE.allowed_sandbox_combos:
        assert pair in grid, f"Free combo {pair} is not a published E2B template"
