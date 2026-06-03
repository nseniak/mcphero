"""Prune persisted data for users / upstreams that are no longer configured.

Runs at startup. Removes per-user and per-upstream entries from connections.json
and per-user entries from oauth_state.json. Does NOT touch the audit log
(it is immutable history).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import structlog

from mcpolis.domain.ports import ADMIN_USER_ID

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# All connections.json key prefixes that include an upstream_id, and whether
# they also include a user_id (and at what colon offset).
_PER_UPSTREAM_KEYS_WITH_USER = ("user:", "client_info:", "pending_code:")
_PER_UPSTREAM_KEYS_NO_USER = ("admin:", "error:", "enabled:")


def _prune_connections_file(
    path: Path,
    valid_emails: set[str],
    valid_upstream_ids: set[str],
) -> tuple[int, int]:
    """Remove stale entries from connections.json.

    Returns (user_removed, upstream_removed).
    """
    if not path.exists():
        return (0, 0)
    try:
        data: dict[str, object] = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "data.prune.read_failed",
            path=str(path),
        )
        return (0, 0)

    user_removed: list[str] = []
    upstream_removed: list[str] = []

    for key in list(data.keys()):
        # Per-upstream + per-user keys
        for prefix in _PER_UPSTREAM_KEYS_WITH_USER:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            colon = rest.find(":")
            if colon < 0:
                break
            upstream_id = rest[:colon]
            user_id = rest[colon + 1:]
            if upstream_id not in valid_upstream_ids:
                upstream_removed.append(key)
            elif user_id != ADMIN_USER_ID and user_id not in valid_emails:
                user_removed.append(key)
            break
        else:
            # Per-upstream-only keys
            for prefix in _PER_UPSTREAM_KEYS_NO_USER:
                if not key.startswith(prefix):
                    continue
                upstream_id = key[len(prefix):]
                if upstream_id not in valid_upstream_ids:
                    upstream_removed.append(key)
                break

    for k in user_removed + upstream_removed:
        del data[k]
    if user_removed or upstream_removed:
        path.write_text(json.dumps(data, indent=2))
    return (len(user_removed), len(upstream_removed))


def _prune_oauth_state_file(path: Path, valid_emails: set[str]) -> int:
    """Remove access/refresh tokens for users not in valid_emails. Returns # removed."""
    if not path.exists():
        return 0
    try:
        raw = cast(dict[str, Any], json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "data.prune.read_failed",
            path=str(path),
        )
        return 0

    removed = 0
    for section in ("access_tokens", "refresh_tokens"):
        bucket = cast(dict[str, dict[str, Any]] | None, raw.get(section))
        if not isinstance(bucket, dict):
            continue
        for token_str in list(bucket.keys()):
            entry = bucket[token_str]
            email = entry.get("user_email")
            if isinstance(email, str) and email not in valid_emails:
                del bucket[token_str]
                removed += 1

    if removed:
        path.write_text(json.dumps(raw, indent=2))
    return removed


def prune_data(
    org_id: str,
    data_dir: Path,
    valid_emails: set[str],
    valid_upstream_ids: set[str],
) -> None:
    """Remove all persisted data for users/upstreams not in the configured sets.

    Operates directly on JSON files, so it must be called BEFORE any store
    or OAuth provider loads them into memory.

    ``org_id`` is accepted for API symmetry with the Mongo backend (Phase 2c)
    but is unused by the file implementation — the file layout is single-org.
    """
    del org_id  # unused in single-org file mode
    user_removed, upstream_removed = _prune_connections_file(
        data_dir / "connections.json", valid_emails, valid_upstream_ids
    )
    oauth_removed = _prune_oauth_state_file(
        data_dir / "oauth_state.json", valid_emails
    )
    logger.info(
        "data.prune.completed",
        user_entries_removed=user_removed,
        upstream_entries_removed=upstream_removed,
        oauth_tokens_removed=oauth_removed,
        valid_user_count=len(valid_emails),
        valid_upstream_count=len(valid_upstream_ids),
    )
