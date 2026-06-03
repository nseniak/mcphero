"""Per-backend startup reconciler for sandbox refs.

Backend can die at any time (deploy, OOM, host reboot). When it does,
sandboxes outlive it: they're running on E2B (or a separate runner
host) and don't know the backend is gone. Without explicit handling
this leaks compute (running orphans) and forgets paused snapshots
(functional regression — next session cold-starts where we expected
warm).

The reconciler runs once per provider at backend boot, before the
manager starts accepting traffic. It cross-references the provider's
view of "my sandboxes" (matched by the ``mcpolis_instance`` metadata
tag) against the durable :class:`SandboxPersistenceRepository`:

- **Live sandboxes** the persistence layer doesn't recognize → kill
  (the local task that owned them is gone; reattaching mid-stream
  produces a zombie session that's worse than a clean cold start).
- **Paused snapshots** the persistence layer recognizes → keep (next
  session for that ``(org, upstream)`` resumes from them).
- **Paused snapshots** the persistence layer doesn't recognize and
  that are older than the GC threshold → delete.
- **Sandboxes / snapshots tagged with a *different*
  ``mcpolis_instance``** → leave alone (multi-instance / blue-green
  safety; the other instance owns them).

See ``internal/plans/currently-mcpolis-runs-stdio-witty-ocean.md``
§"Resilience" for the full failure-mode table.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ReconcileReport(BaseModel):
    """Operator-visible summary of a single reconciler run.

    Surfaced via a one-time SSE event (admin-UI signal) and a
    structured log entry so an op can spot-check that the backend
    came up cleanly. The numbers are documented as "this instance",
    NOT "all of mcpolis" — another instance's refs are deliberately
    untouched.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    mcpolis_instance: str = Field(min_length=1)

    killed_orphan_sandboxes: int = 0
    kept_paused_snapshots: int = 0
    gc_old_unknown_snapshots: int = 0
    skipped_other_instance: int = 0


class SandboxReconciler(Protocol):
    """Per-backend startup reconciler."""

    async def reconcile(self) -> ReconcileReport:
        """Run one reconciliation pass. Idempotent and safe to invoke
        multiple times — a follow-up run after all orphans are
        cleaned up should report zero kills.
        """
        ...


# 30 days, per plan §"Resilience" point 4. Snapshots younger than
# this that aren't recognized in persistence are kept (likely an
# in-flight deploy); older unknowns are GC'd.
DEFAULT_UNKNOWN_SNAPSHOT_GC_DAYS: int = 30


def is_old_unknown(
    *,
    snapshot_created_at: datetime,
    now: datetime,
    gc_days: int = DEFAULT_UNKNOWN_SNAPSHOT_GC_DAYS,
) -> bool:
    """Return True when an unknown paused snapshot is past the GC
    threshold and safe to delete. Caller filters known-recognized
    snapshots out before calling this."""
    return (now - snapshot_created_at) > timedelta(days=gc_days)


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "DEFAULT_UNKNOWN_SNAPSHOT_GC_DAYS",
    "ReconcileReport",
    "SandboxReconciler",
    "is_old_unknown",
    "now_utc",
]
