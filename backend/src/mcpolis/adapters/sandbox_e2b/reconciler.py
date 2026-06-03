"""E2B-side startup reconciler.

Implements :class:`SandboxReconciler` against the
:class:`E2BClient` Protocol + :class:`SandboxPersistenceRepository`.
Cross-references the provider's view of "my sandboxes" (matched by
the ``mcpolis_instance`` metadata tag attached at create time)
against the durable persistence layer; kills running orphans, keeps
recognized paused snapshots, GCs old unknown snapshots.

See plan §"Resilience" point 4.
"""
from __future__ import annotations

import structlog

from mcpolis.adapters.sandbox_e2b.client import (
    E2BClient,
    E2BSandboxInfo,
    E2BSDKError,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_reconciler import (
    DEFAULT_UNKNOWN_SNAPSHOT_GC_DAYS,
    ReconcileReport,
    is_old_unknown,
    now_utc,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class E2BSandboxReconciler:
    """Per-instance reconciler for the E2B backend."""

    def __init__(
        self,
        client: E2BClient,
        persistence: SandboxPersistenceRepository,
        *,
        mcpolis_instance: str,
        gc_days: int = DEFAULT_UNKNOWN_SNAPSHOT_GC_DAYS,
    ) -> None:
        if not mcpolis_instance:
            raise ValueError(
                "E2BSandboxReconciler requires a non-empty mcpolis_instance"
                " — without it the reconciler can't tell its own sandboxes"
                " apart from another instance's",
            )
        self._client = client
        self._persistence = persistence
        self._mcpolis_instance = mcpolis_instance
        self._gc_days = gc_days

    async def reconcile(self) -> ReconcileReport:
        """One-shot reconciliation pass.

        Pulls every E2B sandbox attributed to mcpolis (any instance)
        and the durable persistence view, then categorises:

        - **Different instance** → leave alone (multi-instance safety).
        - **Running, my instance, not in persistence** → kill (orphan
          live sandbox).
        - **Paused, my instance, in persistence** → keep (next session
          resumes from it).
        - **Paused, my instance, not in persistence, older than GC** →
          delete the snapshot.

        The result :class:`ReconcileReport` is logged + returned for
        the operator-visible SSE event the manager wires up.
        """
        try:
            sandboxes = await self._client.list_sandboxes(
                metadata_filter={"mcpolis_instance": self._mcpolis_instance},
            )
        except E2BSDKError:
            logger.warning(
                "sandbox.reconcile.list_failed",
                provider="e2b",
                mcpolis_instance=self._mcpolis_instance,
                exc_info=True,
            )
            return ReconcileReport(
                provider="e2b",
                mcpolis_instance=self._mcpolis_instance,
            )

        # Persistence view: every (org, upstream) ref attributed to
        # mcpolis (any instance). The reconciler only acts on its own
        # instance's refs; the per-info instance check below filters.
        persisted_refs = await self._persistence.list_all_unscoped()
        # Recognized paused snapshot ids = the provider's snapshot ids
        # for paused-state refs we wrote down.
        recognized_paused: set[str] = {
            ref.paused_snapshot_id
            for ref in persisted_refs
            if ref.paused_snapshot_id is not None
            and ref.provider == "e2b"
        }

        report_kw: dict[str, int] = {
            "killed_orphan_sandboxes": 0,
            "kept_paused_snapshots": 0,
            "gc_old_unknown_snapshots": 0,
            "skipped_other_instance": 0,
        }
        now = now_utc()
        for info in sandboxes:
            tag = info.metadata.get("mcpolis_instance", "")
            if tag != self._mcpolis_instance:
                report_kw["skipped_other_instance"] += 1
                continue
            if info.state == "running":
                await self._kill_orphan(info, report_kw)
            elif info.state == "paused":
                if info.sandbox_id in recognized_paused:
                    report_kw["kept_paused_snapshots"] += 1
                elif is_old_unknown(
                    snapshot_created_at=info.created_at,
                    now=now,
                    gc_days=self._gc_days,
                ):
                    await self._gc_unknown(info, report_kw)
                # Else: unknown but young — likely an in-flight deploy.
                # Leave it; a later reconcile pass will GC if it ages.

        report = ReconcileReport(
            provider="e2b",
            mcpolis_instance=self._mcpolis_instance,
            **report_kw,
        )
        logger.info(
            "sandbox.reconcile.report",
            provider="e2b",
            mcpolis_instance=self._mcpolis_instance,
            **report_kw,
        )
        return report

    async def _kill_orphan(
        self, info: E2BSandboxInfo, report_kw: dict[str, int],
    ) -> None:
        try:
            await self._client.kill_sandbox(info.sandbox_id)
            report_kw["killed_orphan_sandboxes"] += 1
            logger.info(
                "sandbox.reconcile.killed_orphan",
                provider="e2b",
                sandbox_id=info.sandbox_id,
            )
        except E2BSDKError:
            logger.warning(
                "sandbox.reconcile.kill_failed",
                provider="e2b", sandbox_id=info.sandbox_id, exc_info=True,
            )

    async def _gc_unknown(
        self, info: E2BSandboxInfo, report_kw: dict[str, int],
    ) -> None:
        try:
            await self._client.delete_snapshot(info.sandbox_id)
            report_kw["gc_old_unknown_snapshots"] += 1
            logger.info(
                "sandbox.reconcile.gc_unknown",
                provider="e2b",
                snapshot_id=info.sandbox_id,
                created_at=info.created_at.isoformat(),
            )
        except E2BSDKError:
            logger.warning(
                "sandbox.reconcile.gc_failed",
                provider="e2b", snapshot_id=info.sandbox_id, exc_info=True,
            )


__all__ = ["E2BSandboxReconciler"]
