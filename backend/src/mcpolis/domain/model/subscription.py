"""Org-level subscription / plan information.

Kept as a separate object so future billing fields
(``stripe_customer_id``, ``current_period_end``,
``payment_method_last4``, etc.) don't bloat ``Organization``.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class PlanName(str, Enum):
    free = "free"
    team = "team"


class Subscription(BaseModel):
    plan: PlanName = PlanName.free
