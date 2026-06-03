"""Typed event model for the real-time event bus."""
from __future__ import annotations

import time

from pydantic import BaseModel, Field


class Event(BaseModel):
    type: str
    user_email: str | None = None  # None = broadcast to all
    payload: dict[str, object] = {}
    timestamp: float = Field(default_factory=time.time)
