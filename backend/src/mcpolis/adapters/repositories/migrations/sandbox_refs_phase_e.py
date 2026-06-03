"""Phase E one-shot migration for the ``sandbox_refs`` collection.

Brings every document in ``sandbox_refs`` to the strict shape that
``MongoSandboxPersistenceRepository._from_doc`` accepts after the
Phase E persistence consolidation. After this script runs, the
``connections`` collection's ``enabled:<id>`` rows are NOT touched
(Phase E collapses the tristate by renaming, not by data migration —
see plan §"Persistence: one read, one write, one shape").

Three outcomes per doc:

* **unchanged** — the doc already has the strict shape (every field
  present, every value the right type). The script writes nothing.
* **upgraded** — the doc is missing one or more nullable fields
  (``pid``, ``sandbox_id``, ``paused_snapshot_id``,
  ``cached_server_info``, ``cached_self_description``) or
  ``metadata``. The script fills the missing fields with their
  domain-canonical defaults (``None`` for nullable, ``{}`` for
  metadata) and writes the upgraded doc back. After this, the doc
  passes ``_from_doc`` cleanly.
* **dropped** — the doc is missing a non-recoverable required field
  (``provider``, ``org_id``, ``upstream_id``, ``mcpolis_instance``,
  ``last_updated``) or has a value of the wrong type that we can't
  safely repair. Such docs would log WARNING storms under v2 if left
  in place; the safer action is to drop and let the per-backend
  reconciler handle the resulting orphan sandbox / snapshot.

# Reversible-deploy story

1. Deploy v1 (this PR) — strict-read + strict-write. Old docs that
   don't match still work because the boundary catches and skips
   them, but they're unreachable. Coexists with the migration
   script.
2. Take a backup (one-shot, manual; see below).
3. Run the migration in dry-run mode against prod. Eyeball the
   counts. If the "would-drop" count is non-zero, eyeball the docs
   themselves before running for real.
4. Run the migration for real.
5. Deploy v2 (a follow-up PR if/when needed) that removes any
   remaining v1 lenient back-compat code. Today there is none —
   this PR is already strict — so step 5 is informational.

# Backup before running

Run on the host (NOT this script's host):

    ssh seniak-ec2 "docker exec mcpolis-mongo mongodump \\
        --db mcpolis \\
        --collection sandbox_refs \\
        --out /tmp/sandbox_refs_pre_phase_e_$(date +%%Y%%m%%d_%%H%%M%%S)"

To restore (if the migration's drop count is wrong):

    ssh seniak-ec2 "docker exec mcpolis-mongo mongorestore \\
        --drop --nsInclude='mcpolis.sandbox_refs' \\
        /tmp/sandbox_refs_pre_phase_e_<TIMESTAMP>"

This is a one-shot — there is no ``make mcpolis-mongodump`` target.
Honest about the convention: when the next migration needs a
backup, repeat the pattern; if it becomes routine, lift it into
seniak-infra at that point.

# Running

Local (against ``MCPOLIS_MONGO_URI``):

    MIGRATIONS_DRY_RUN=true \\
        python -m mcpolis.adapters.repositories.migrations.sandbox_refs_phase_e

Real run:

    MIGRATIONS_DRY_RUN=false \\
        python -m mcpolis.adapters.repositories.migrations.sandbox_refs_phase_e

In prod, the convention is to run inside the backend container:

    ssh seniak-ec2 "cd ~/mcpolis && docker compose \\
        --env-file .env.cloud.docker.prod \\
        -f docker-compose.yml -f docker-compose.proxied.yml \\
        --profile cloud exec backend \\
        python -m mcpolis.adapters.repositories.migrations.sandbox_refs_phase_e"

with ``MIGRATIONS_DRY_RUN=true`` toggled via ``-e`` on the
``compose exec`` command for the dry-run pass.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

import structlog

from mcpolis.adapters.repositories.mongo_client import (
    COLL_SANDBOX_REFS,
    MongoConnection,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    MalformedRefError,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# Fields the script can fill in for a missing-but-recoverable doc.
# Every nullable field on ``SandboxPersistedRef`` plus ``metadata``
# (which has an empty-dict canonical default).
_NULLABLE_FIELDS: tuple[str, ...] = (
    "sandbox_id",
    "paused_snapshot_id",
    "pid",
    "cached_server_info",
    "cached_self_description",
)

# Fields that have no safe default — if missing the doc must be
# dropped. Listed for documentation; the classifier just relies on
# ``_from_doc`` rejecting the upgraded shape.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "provider",
    "org_id",
    "upstream_id",
    "mcpolis_instance",
    "last_updated",
)


@dataclass(frozen=True)
class Plan:
    """Per-doc outcome.

    Either ``upgrade_to`` is the doc to write back (replacing the
    original), ``drop`` is True (delete the doc), or both are unset
    (unchanged — no IO).
    """

    upstream_id: str
    org_id: str | None
    kind: str  # "unchanged" | "upgraded" | "dropped"
    upgrade_to: dict[str, Any] | None = None
    reason: str | None = None  # populated for "dropped"


def classify_doc(doc: dict[str, Any]) -> Plan:
    """Pure classifier — given a raw Mongo doc, decide what to do.

    Strategy: try ``_from_doc`` as-is. If it accepts, return
    ``unchanged``. If it raises ``MalformedRefError`` and the bad
    field is in ``_NULLABLE_FIELDS`` (or ``metadata``), upgrade the
    doc by filling the missing field and re-try ``_from_doc``.
    Repeat. If we run out of upgradable fields or the error names a
    non-recoverable field, drop.
    """
    upstream_id = str(doc.get("upstream_id", "<unknown>"))
    org_id_val = doc.get("org_id")
    org_id = str(org_id_val) if isinstance(org_id_val, str) else None

    candidate = dict(doc)
    upgraded = False
    for _ in range(len(_NULLABLE_FIELDS) + 1):  # bounded fixpoint
        try:
            MongoSandboxPersistenceRepository.from_doc(candidate)
        except MalformedRefError as exc:
            if exc.field == "metadata" and exc.reason == "missing":
                candidate["metadata"] = {}
                upgraded = True
                continue
            if exc.field in _NULLABLE_FIELDS and exc.reason == "missing":
                candidate[exc.field] = None
                upgraded = True
                continue
            return Plan(
                upstream_id=upstream_id,
                org_id=org_id,
                kind="dropped",
                reason=f"{exc.field}: {exc.reason}",
            )
        else:
            if upgraded:
                return Plan(
                    upstream_id=upstream_id,
                    org_id=org_id,
                    kind="upgraded",
                    upgrade_to=candidate,
                )
            return Plan(
                upstream_id=upstream_id,
                org_id=org_id,
                kind="unchanged",
            )
    return Plan(
        upstream_id=upstream_id,
        org_id=org_id,
        kind="dropped",
        reason="exceeded upgrade fixpoint (likely loop in classifier)",
    )


@dataclass
class Summary:
    unchanged: int = 0
    upgraded: int = 0
    dropped: int = 0


async def run_migration(
    *, mongo_uri: str, mongo_db: str, dry_run: bool,
) -> Summary:
    """Connect to Mongo, classify every doc, apply the plans."""
    summary = Summary()
    mongo = MongoConnection(mongo_uri, mongo_db)
    try:
        coll = mongo.database[COLL_SANDBOX_REFS]
        cursor = coll.find({})
        async for doc in cursor:
            plan = classify_doc(doc)
            if plan.kind == "unchanged":
                summary.unchanged += 1
                continue
            if plan.kind == "upgraded":
                summary.upgraded += 1
                logger.info(
                    "migration.sandbox_refs.upgrade",
                    org_id=plan.org_id,
                    upstream_id=plan.upstream_id,
                    dry_run=dry_run,
                )
                if not dry_run and plan.upgrade_to is not None:
                    await coll.replace_one(
                        {"_id": doc["_id"]},
                        plan.upgrade_to,
                    )
                continue
            # dropped
            summary.dropped += 1
            logger.warning(
                "migration.sandbox_refs.drop",
                org_id=plan.org_id,
                upstream_id=plan.upstream_id,
                reason=plan.reason,
                dry_run=dry_run,
            )
            if not dry_run:
                await coll.delete_one({"_id": doc["_id"]})
    finally:
        mongo.close()
    return summary


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


async def _main() -> int:
    mongo_uri = os.environ.get("MCPOLIS_MONGO_URI", "").strip()
    mongo_db = os.environ.get("MCPOLIS_MONGO_DB_NAME", "mcpolis").strip()
    if not mongo_uri:
        print(
            "MCPOLIS_MONGO_URI is required.",
            file=sys.stderr,
        )
        return 2
    dry_run = _bool_env("MIGRATIONS_DRY_RUN", default=True)

    logger.info(
        "migration.sandbox_refs.start",
        mongo_db=mongo_db,
        dry_run=dry_run,
    )
    summary = await run_migration(
        mongo_uri=mongo_uri, mongo_db=mongo_db, dry_run=dry_run,
    )
    logger.info(
        "migration.sandbox_refs.done",
        unchanged=summary.unchanged,
        upgraded=summary.upgraded,
        dropped=summary.dropped,
        dry_run=dry_run,
    )
    print(
        f"sandbox_refs migration ({'dry-run' if dry_run else 'real'}): "
        f"unchanged={summary.unchanged} "
        f"upgraded={summary.upgraded} "
        f"dropped={summary.dropped}",
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
