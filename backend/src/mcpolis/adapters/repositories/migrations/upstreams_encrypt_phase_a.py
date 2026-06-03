"""One-shot migration: encrypt the ``upstreams`` collection at rest.

Before this migration, ``upstreams`` documents stored
``server_config`` and ``options`` as nested plaintext sub-documents.
Both carry user-supplied credential material (stdio command/args/env,
HTTP url/headers, OAuth client_id/client_secret, scopes), which means
a leaked Mongo backup or read-only operator session exposes every
upstream's credentials.

After this migration, each row carries:

* ``server_config_encrypted`` — the JSON-serialized server config,
  stored as a single AES-256-GCM-encrypted string blob.
* ``options_encrypted`` — the JSON-serialized options, same encoding.
* The legacy plaintext ``server_config`` and ``options`` keys are
  ``$unset``.

The same script also strips a stale plaintext copy that lived on
some ``config`` documents: cloud mode used to also push upstream
options into ``config.upstreams[*]`` via the now-deprecated
``MongoConfigRepository.set_upstream_options`` path. That code is
gone, but pre-existing documents may still carry the array. We
``$unset`` ``config.upstreams`` from every ``config`` row so the
encrypted truth in the ``upstreams`` collection is the only copy.

Three outcomes per ``upstreams`` doc:

* **already_encrypted** — the doc already has the new shape (both
  encrypted keys present, no plaintext keys). The script writes
  nothing. Re-running is a no-op.
* **encrypted** — the doc still has plaintext ``server_config`` /
  ``options`` (or one of them). The script JSON-serializes each
  present key, writes the resulting strings under
  ``server_config_encrypted`` / ``options_encrypted`` (the
  ``OrgScopedCollection`` wrapper auto-encrypts), and ``$unset``s
  the old keys.
* **skipped** — the doc has neither plaintext nor encrypted shape;
  it's malformed and the migration leaves it untouched. The reader
  in ``MongoUpstreamConfigRepository._load_one`` already drops
  malformed rows.

# Reversible-deploy story

This migration is irreversible without re-deriving the encryption
key — ciphertext is opaque. Take a backup before running for real.

    ssh seniak-ec2 "docker exec mcpolis-mongo mongodump \\
        --db mcpolis \\
        --collection upstreams \\
        --out /tmp/upstreams_pre_encrypt_$(date +%%Y%%m%%d_%%H%%M%%S)"
    ssh seniak-ec2 "docker exec mcpolis-mongo mongodump \\
        --db mcpolis \\
        --collection config \\
        --out /tmp/config_pre_encrypt_$(date +%%Y%%m%%d_%%H%%M%%S)"

# Running

Local (against ``MCPOLIS_MONGO_URI``, ``MCPOLIS_ENCRYPTION_KEY``):

    MIGRATIONS_DRY_RUN=true \\
        python -m mcpolis.adapters.repositories.migrations.upstreams_encrypt_phase_a

Real run:

    MIGRATIONS_DRY_RUN=false \\
        python -m mcpolis.adapters.repositories.migrations.upstreams_encrypt_phase_a

In prod, run inside the backend container:

    ssh seniak-ec2 "cd ~/mcpolis && docker compose \\
        --env-file .env.cloud.docker.prod \\
        -f docker-compose.yml -f docker-compose.proxied.yml \\
        --profile cloud exec backend \\
        python -m mcpolis.adapters.repositories.migrations.upstreams_encrypt_phase_a"

The dry-run pass should be reviewed before the real run — once the
real run completes, plaintext copies are gone.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import structlog

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    COLL_UPSTREAMS,
    MongoConnection,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class UpstreamsSummary:
    already_encrypted: int = 0
    encrypted: int = 0
    skipped: int = 0


@dataclass
class ConfigSummary:
    cleaned: int = 0  # docs that had a stale ``upstreams`` field unset
    untouched: int = 0


def _encode_blob(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(k, str):
            out[k] = v
    return out


async def _migrate_upstreams(
    coll: Any, encryptor: FieldEncryptor, *, dry_run: bool,
) -> UpstreamsSummary:
    summary = UpstreamsSummary()
    cursor = coll.find({})
    async for doc in cursor:
        has_encrypted = (
            isinstance(doc.get("server_config_encrypted"), str)
            and isinstance(doc.get("options_encrypted"), str)
        )
        plaintext_sc = _coerce_dict(doc.get("server_config"))
        plaintext_opts = _coerce_dict(doc.get("options"))
        has_plaintext = plaintext_sc is not None or plaintext_opts is not None

        if has_encrypted and not has_plaintext:
            summary.already_encrypted += 1
            continue

        if not has_plaintext:
            summary.skipped += 1
            logger.warning(
                "migration.upstreams_encrypt.skip",
                upstream_id=str(doc.get("upstream_id", "<unknown>")),
                org_id=str(doc.get("org_id", "<unknown>")),
                reason="no plaintext or encrypted payload found",
                dry_run=dry_run,
            )
            continue

        # Build the new shape. Encrypt at the application layer here;
        # we deliberately bypass ``OrgScopedCollection`` because the
        # migration runs across orgs and over already-stored documents
        # whose ``org_id`` we trust as-is.
        sc_blob = _encode_blob(plaintext_sc or {})
        opts_blob = _encode_blob(plaintext_opts or {})
        encrypted_doc_set: dict[str, Any] = {
            "server_config_encrypted": encryptor.encrypt_string(sc_blob),
            "options_encrypted": encryptor.encrypt_string(opts_blob),
        }
        unset_keys: dict[str, str] = {}
        if "server_config" in doc:
            unset_keys["server_config"] = ""
        if "options" in doc:
            unset_keys["options"] = ""

        summary.encrypted += 1
        logger.info(
            "migration.upstreams_encrypt.encrypt",
            upstream_id=str(doc.get("upstream_id", "<unknown>")),
            org_id=str(doc.get("org_id", "<unknown>")),
            had_server_config=plaintext_sc is not None,
            had_options=plaintext_opts is not None,
            dry_run=dry_run,
        )
        if not dry_run:
            update: dict[str, Any] = {"$set": encrypted_doc_set}
            if unset_keys:
                update["$unset"] = unset_keys
            await coll.update_one({"_id": doc["_id"]}, update)
    return summary


async def _strip_stale_config_upstreams(
    coll: Any, *, dry_run: bool,
) -> ConfigSummary:
    summary = ConfigSummary()
    cursor = coll.find({})
    async for doc in cursor:
        config_blob = doc.get("config")
        if not isinstance(config_blob, dict):
            summary.untouched += 1
            continue
        if "upstreams" not in config_blob:
            summary.untouched += 1
            continue
        # Some pre-existing docs may also have ``upstreams`` at the
        # outer level (older shape). Strip both if present.
        unset_keys: dict[str, str] = {"config.upstreams": ""}
        if "upstreams" in doc:
            unset_keys["upstreams"] = ""
        summary.cleaned += 1
        logger.info(
            "migration.upstreams_encrypt.config_strip",
            org_id=str(doc.get("org_id", "<unknown>")),
            stripped_keys=list(unset_keys.keys()),
            dry_run=dry_run,
        )
        if not dry_run:
            await coll.update_one(
                {"_id": doc["_id"]}, {"$unset": unset_keys},
            )
    return summary


async def run_migration(
    *,
    mongo_uri: str,
    mongo_db: str,
    encryption_key: str,
    dry_run: bool,
) -> tuple[UpstreamsSummary, ConfigSummary]:
    encryptor = FieldEncryptor.from_master_secret(encryption_key)
    mongo = MongoConnection(mongo_uri, mongo_db)
    try:
        upstreams_coll = mongo.database[COLL_UPSTREAMS]
        config_coll = mongo.database[COLL_CONFIG]
        upstreams_summary = await _migrate_upstreams(
            upstreams_coll, encryptor, dry_run=dry_run,
        )
        config_summary = await _strip_stale_config_upstreams(
            config_coll, dry_run=dry_run,
        )
    finally:
        mongo.close()
    return upstreams_summary, config_summary


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


async def _main() -> int:
    mongo_uri = os.environ.get("MCPOLIS_MONGO_URI", "").strip()
    mongo_db = os.environ.get("MCPOLIS_MONGO_DB_NAME", "mcpolis").strip()
    encryption_key = os.environ.get("MCPOLIS_ENCRYPTION_KEY", "").strip()
    if not mongo_uri:
        print("MCPOLIS_MONGO_URI is required.", file=sys.stderr)
        return 2
    if not encryption_key:
        print("MCPOLIS_ENCRYPTION_KEY is required.", file=sys.stderr)
        return 2
    dry_run = _bool_env("MIGRATIONS_DRY_RUN", default=True)

    logger.info(
        "migration.upstreams_encrypt.start",
        mongo_db=mongo_db,
        dry_run=dry_run,
    )
    upstreams_summary, config_summary = await run_migration(
        mongo_uri=mongo_uri,
        mongo_db=mongo_db,
        encryption_key=encryption_key,
        dry_run=dry_run,
    )
    logger.info(
        "migration.upstreams_encrypt.done",
        upstreams_already_encrypted=upstreams_summary.already_encrypted,
        upstreams_encrypted=upstreams_summary.encrypted,
        upstreams_skipped=upstreams_summary.skipped,
        config_cleaned=config_summary.cleaned,
        config_untouched=config_summary.untouched,
        dry_run=dry_run,
    )
    print(
        f"upstreams_encrypt migration ({'dry-run' if dry_run else 'real'}): "
        f"upstreams already_encrypted={upstreams_summary.already_encrypted} "
        f"encrypted={upstreams_summary.encrypted} "
        f"skipped={upstreams_summary.skipped} | "
        f"config cleaned={config_summary.cleaned} "
        f"untouched={config_summary.untouched}",
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
