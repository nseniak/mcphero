"""Per-backend startup reconciler for sandbox refs.

Backend can die at any time (deploy, OOM, host reboot). When it does,
sandboxes outlive it: they're running on E2B (or a separate runner
host) and don't know the backend is gone. Without explicit handling
this leaks compute (running orphans) and storage (paused ones).

The reconciler runs once per provider at backend boot, before any MCP
connects and before the manager starts accepting traffic. It
cross-references the provider's view of "my sandboxes" (matched by the
``mcpolis_instance`` metadata tag) against the durable
:class:`SandboxPersistenceRepository`:

- **Sandboxes** (running or paused) a persisted ref points at → keep
  (the next session for that ``(org, upstream)`` reuses the sandbox,
  with a fresh MCP process).
- **Running sandboxes** no ref points at → kill.
- **Paused sandboxes** no ref points at and older than
  :data:`DEFAULT_PAUSED_ORPHAN_GRACE` → kill.
- **Sandboxes tagged with a *different* ``mcpolis_instance``** → leave
  alone: they belong to another environment (another database) that
  shares the E2B account.

The ``mcpolis_instance`` id is one value per database, not per process
(``SandboxPersistenceRepository.get_or_create_instance_id``), and one
backend runs against a database at a time: a deploy stops the old
container before it starts the new one (fixed ``container_name``). So
when the reconciler runs, nothing alive owns a sandbox that no ref
points at. That is also its limit: two backends on one database at once
(blue-green) would look like one instance, and the new one's reconcile
would kill a sandbox the old one is still creating.

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
    kept_tracked_sandboxes: int = 0
    kept_paused_snapshots: int = 0
    gc_old_unknown_snapshots: int = 0
    skipped_other_instance: int = 0
    # Sandboxes and volumes of removed upstreams whose kill or destroy
    # failed at the removal, killed or destroyed (or found gone) by this
    # run.
    killed_leftover_sandboxes: int = 0
    destroyed_leftover_volumes: int = 0


class SandboxReconciler(Protocol):
    """Per-backend startup reconciler."""

    async def reconcile(self) -> ReconcileReport:
        """Run one reconciliation pass. Idempotent and safe to invoke
        multiple times — a follow-up run after all orphans are
        cleaned up should report zero kills.
        """
        ...


# How long a paused sandbox that no ref points at is kept before the
# boot cleanup kills it. One hour, down from 30 days:
#
# - Nearly every orphan is paused by the time a boot sees it. Sandboxes
#   are created with ``on_timeout=pause``, so one nobody uses pauses
#   60-300 s after its last refresh. With 30 days, orphans lived a month.
# - At boot nothing alive owns a sandbox no ref points at (see the module
#   docstring), so the grace only has to cover clock skew between E2B's
#   ``started_at`` and ours, and two backends overlapping during a deploy
#   should that ever happen (a 90 s container stop grace that includes
#   the 30 s drain). That is minutes, not hours.
# - It matches the 1 h ``--age-min-hours`` default of
#   ``tests/integration/list_orphan_sandboxes.py``, the operator's
#   manual cleanup, so the two agree on what "too young to kill" means.
# - Boots are hours to days apart, so an orphan younger than this at
#   one boot is killed at the next one.
DEFAULT_PAUSED_ORPHAN_GRACE: timedelta = timedelta(hours=1)


def is_old_unknown(
    *,
    snapshot_created_at: datetime,
    now: datetime,
    grace: timedelta = DEFAULT_PAUSED_ORPHAN_GRACE,
) -> bool:
    """Return True when a paused sandbox no ref points at is past the
    grace and safe to kill. Caller filters recognized sandboxes out
    before calling this."""
    return (now - snapshot_created_at) > grace


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "DEFAULT_PAUSED_ORPHAN_GRACE",
    "ReconcileReport",
    "SandboxReconciler",
    "is_old_unknown",
    "now_utc",
]
