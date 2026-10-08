"""E2B-side startup reconciler.

Implements :class:`SandboxReconciler` against the
:class:`E2BClient` Protocol + :class:`SandboxPersistenceRepository`.
Cross-references the provider's view of "my sandboxes" (matched by
the ``mcpolis_instance`` metadata tag attached at create time)
against the durable persistence layer; keeps every sandbox a ref
points at, kills running orphans and paused orphans past the grace,
and retries the sandbox kills and volume destroys a removal could not
do.

See plan §"Resilience" point 4.
"""
from __future__ import annotations

from datetime import timedelta

import structlog

from mcpolis.adapters.sandbox_e2b.client import (
    E2BClient,
    E2BNotFoundError,
    E2BSandboxInfo,
    E2BSDKError,
)
from mcpolis.adapters.sandbox_e2b.service import (
    destroy_volumes,
    kill_sandboxes,
    sandboxes_to_kill,
    volumes_to_destroy,
    with_leftovers_gone,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)
from mcpolis.domain.services.sandbox_reconciler import (
    DEFAULT_PAUSED_ORPHAN_GRACE,
    ReconcileReport,
    is_old_unknown,
    now_utc,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class E2BSandboxReconciler:
    """Boot-time reconciler for the E2B backend."""

    def __init__(
        self,
        client: E2BClient,
        persistence: SandboxPersistenceRepository,
        *,
        mcpolis_instance: str,
        paused_orphan_grace: timedelta = DEFAULT_PAUSED_ORPHAN_GRACE,
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
        self._paused_orphan_grace = paused_orphan_grace

    async def reconcile(self) -> ReconcileReport:
        """One-shot reconciliation pass.

        Pulls every E2B sandbox tagged with this instance id and the
        durable persistence view, then categorises:

        - **Different instance** → leave alone: another environment
          (another database) sharing the E2B account owns it.
        - **A persisted ref points at it** (running or paused) → keep.
          A running one is a sandbox preserved across the last
          shutdown; the next connect reuses it, and killing it would
          throw away its package cache. A paused one is what the next
          wake resumes.
        - **Running, no ref** → kill (orphan).
        - **Paused, no ref, older than the grace** → kill (orphan).

        Then it kills the sandboxes refs list as still to kill, by id and
        whatever instance tag they carry, and destroys the volumes refs
        list as still to destroy (a removal, an org deletion or the
        fresh-sandboxes override whose kill or destroy failed), and drops
        them from the refs. A sandbox made before the instance id became
        one value per database carries a tag the listing above never
        returns: such a ref is the only thing that still names it.

        Run it only before this process opens any sandbox, as the boot
        does: it reads every sandbox no ref points at as left behind by
        a process that is gone. A start in flight has no ref yet, so a
        reconcile running alongside one would kill its sandbox. The
        instance id is stable per database (see
        ``SandboxPersistenceRepository.get_or_create_instance_id``), so
        this sees what previous processes left behind, including the
        sandbox of a start that a crash cut short.

        Never raises for a provider or store failure: it logs and
        returns what it did, and the boot goes on.

        The result :class:`ReconcileReport` is logged + returned for
        the operator-visible SSE event the manager wires up.
        """
        empty = ReconcileReport(
            provider="e2b", mcpolis_instance=self._mcpolis_instance,
        )
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
            return empty

        # Persistence view: every (org, upstream) ref attributed to
        # mcpolis (any instance). Without it every sandbox would look
        # like an orphan, so a failed read reconciles nothing.
        try:
            persisted_refs = await self._persistence.list_all_unscoped()
        except Exception:
            logger.warning(
                "sandbox.reconcile.refs_read_failed",
                provider="e2b",
                mcpolis_instance=self._mcpolis_instance,
                exc_info=True,
            )
            return empty
        # Every provider id a ref points at. E2B's own idle pause keeps
        # the sandbox id, so a paused sandbox is usually recognized by
        # ``sandbox_id``; ``paused_snapshot_id`` covers explicit pause.
        recognized: set[str] = set()
        for ref in persisted_refs:
            if ref.provider != "e2b":
                continue
            if ref.sandbox_id is not None:
                recognized.add(ref.sandbox_id)
            if ref.paused_snapshot_id is not None:
                recognized.add(ref.paused_snapshot_id)

        report_kw: dict[str, int] = {
            "killed_orphan_sandboxes": 0,
            "kept_tracked_sandboxes": 0,
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
                if info.sandbox_id in recognized:
                    report_kw["kept_tracked_sandboxes"] += 1
                    continue
                await self._kill_orphan(info, report_kw)
            elif info.state == "paused":
                if info.sandbox_id in recognized:
                    report_kw["kept_paused_snapshots"] += 1
                elif is_old_unknown(
                    snapshot_created_at=info.created_at,
                    now=now,
                    grace=self._paused_orphan_grace,
                ):
                    await self._gc_unknown(info, report_kw)
                # Else: paused, no ref, younger than the grace. Kept
                # until a later boot; see DEFAULT_PAUSED_ORPHAN_GRACE.
        (
            report_kw["killed_leftover_sandboxes"],
            report_kw["destroyed_leftover_volumes"],
        ) = await self._clean_up_leftovers(persisted_refs)

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

    async def _clean_up_leftovers(
        self, refs: list[SandboxPersistedRef],
    ) -> tuple[int, int]:
        """Retry the sandbox kills and volume destroys a removal could
        not do (``SANDBOXES_TO_KILL_METADATA_KEY``,
        ``VOLUMES_TO_DESTROY_METADATA_KEY``), and stop listing the ones
        now gone. Returns how many sandboxes, then how many volumes,
        went."""
        killed_count = 0
        destroyed_count = 0
        for ref in refs:
            if ref.provider != "e2b":
                continue
            sandboxes = sandboxes_to_kill(ref)
            volumes = volumes_to_destroy(ref)
            if not sandboxes and not volumes:
                continue
            sandboxes_left = await kill_sandboxes(
                self._client, sandboxes,
                org_id=ref.org_id, upstream_id=ref.upstream_id,
            )
            volumes_left = await destroy_volumes(
                self._client, volumes,
                org_id=ref.org_id, upstream_id=ref.upstream_id,
            )
            killed = [s for s in sandboxes if s not in sandboxes_left]
            destroyed = [v for v in volumes if v not in volumes_left]
            if not killed and not destroyed:
                continue
            killed_count += len(killed)
            destroyed_count += len(destroyed)
            await self._forget_leftovers(ref, killed, destroyed)
        return killed_count, destroyed_count

    async def _forget_leftovers(
        self,
        ref: SandboxPersistedRef,
        killed: list[str],
        destroyed: list[str],
    ) -> None:
        """Drop ``killed`` and ``destroyed`` from what ``ref``'s upstream
        still has to clean up; the ref goes once nothing is left on it.
        Read again first: the ref is the upstream's own, which an upstream
        added again under the same id also writes."""
        try:
            current = await self._persistence.get(
                org_id=ref.org_id, upstream_id=ref.upstream_id,
            )
            if current is None:
                return
            updated = with_leftovers_gone(
                current, volumes=destroyed, sandboxes=killed,
            )
            if updated is None:
                await self._persistence.delete(
                    org_id=ref.org_id, upstream_id=ref.upstream_id,
                )
            else:
                await self._persistence.upsert(updated)
        except Exception:
            # Listed again at the next boot, whose kill or destroy then
            # finds it gone: harmless.
            logger.warning(
                "sandbox.reconcile.leftover_forget_failed",
                provider="e2b",
                org_id=ref.org_id,
                upstream_id=ref.upstream_id,
                exc_info=True,
            )

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
        except E2BNotFoundError:
            # Gone between the list and the kill: the outcome we wanted.
            logger.info(
                "sandbox.reconcile.already_gone",
                provider="e2b", sandbox_id=info.sandbox_id,
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
            # A paused sandbox is removed by killing it. The SDK's
            # ``delete_snapshot`` deletes a TEMPLATE: given a sandbox
            # id it answers not-found without raising, so the GC
            # reported success and removed nothing.
            await self._client.kill_sandbox(info.sandbox_id)
            report_kw["gc_old_unknown_snapshots"] += 1
            logger.info(
                "sandbox.reconcile.gc_unknown",
                provider="e2b",
                snapshot_id=info.sandbox_id,
                created_at=info.created_at.isoformat(),
            )
        except E2BNotFoundError:
            logger.info(
                "sandbox.reconcile.already_gone",
                provider="e2b", sandbox_id=info.sandbox_id,
            )
        except E2BSDKError:
            logger.warning(
                "sandbox.reconcile.gc_failed",
                provider="e2b", snapshot_id=info.sandbox_id, exc_info=True,
            )


__all__ = ["E2BSandboxReconciler"]
