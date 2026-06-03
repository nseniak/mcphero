"""Single source of truth for what each plan allows.

Used by every backend gate so policy never drifts: every place that
needs to know a limit consults ``limits_for(plan)``.
"""
from __future__ import annotations

from dataclasses import dataclass

from mcpolis.domain.model.subscription import PlanName


class PlanLimitExceeded(Exception):
    """Raised by route handlers when an action would cross a plan
    limit. The FastAPI exception handler maps this to HTTP 402 with a
    structured JSON body the frontend uses to drive the upgrade
    modal.
    """

    def __init__(
        self,
        gate: str,
        current: int | None,
        limit: int | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.gate = gate
        self.current = current
        self.limit = limit
        self.message = message


@dataclass(frozen=True)
class PlanLimits:
    max_seats: int | None
    max_http_upstreams: int | None
    max_stdio_upstreams: int | None
    max_custom_roles: int | None
    allow_argument_constraints: bool
    audit_retention_days: int
    allowed_sandbox_combos: tuple[tuple[int, int], ...] | None


FREE = PlanLimits(
    max_seats=3,
    max_http_upstreams=5,
    max_stdio_upstreams=1,
    max_custom_roles=0,
    allow_argument_constraints=False,
    audit_retention_days=30,
    allowed_sandbox_combos=((1, 1024),),
)


TEAM = PlanLimits(
    max_seats=None,
    max_http_upstreams=None,
    max_stdio_upstreams=None,
    max_custom_roles=None,
    allow_argument_constraints=True,
    audit_retention_days=365,
    allowed_sandbox_combos=None,
)


def limits_for(plan: PlanName) -> PlanLimits:
    if plan == PlanName.free:
        return FREE
    if plan == PlanName.team:
        return TEAM
    raise ValueError(f"Unknown plan: {plan}")
