"""List E2B sandboxes attributed to mcpolis and categorise them.

Read-only by default; pass ``--delete-orphans`` to kill anything
that isn't ``recognized``. Use ``--age-min-hours N`` (default 1)
to avoid friendly-firing sandboxes that another instance booted
in the last N hours.

Categories:

- ``recognized``: in Mongo persistence with a matching live
  ``sandbox_id``. Mcpolis is using this sandbox right now (or
  intends to reattach to it next boot). **Never** killed by
  ``--delete-orphans``.
- ``mine_orphan``: ``mcpolis_instance`` metadata tag matches the
  ``--mcpolis-instance`` arg (or the running mcpolis's instance,
  if introspectable) but no persisted ref exists. Likely a crash
  before the persist write, or persistence got truncated.
- ``foreign_instance``: ``mcpolis_instance`` tag points to a
  different instance. Could be a live blue/green peer (don't
  delete) OR an old crashed instance whose tag we can't reclaim.
  The ``--age-min-hours`` gate is the heuristic separator.
- ``untagged``: no ``mcpolis_instance`` metadata at all. Manually
  created or pre-instance-tagging legacy. Treated as orphan.

Run with::

    bash backend/tests/integration/run-list-orphan-sandboxes.sh
    bash backend/tests/integration/run-list-orphan-sandboxes.sh --delete-orphans
    bash backend/tests/integration/run-list-orphan-sandboxes.sh --json

Reads ``MCPOLIS_E2B_API_KEY`` from env (or the gitignored prod
secrets file via the wrapper script). For the persistence
cross-reference, also reads Mongo settings the same way the
backend does — set ``MCPOLIS_MONGO_URI`` etc. for cloud-mode
inspection, or omit them to skip the Mongo cross-ref entirely
(every sandbox then categorises by tag/age only).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from mcpolis.adapters.sandbox_e2b import RealE2BClient  # noqa: E402
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError  # noqa: E402

API_KEY = os.environ.get("MCPOLIS_E2B_API_KEY") or os.environ.get("E2B_API_KEY")


@dataclass
class _Categorised:
    sandbox_id: str
    state: str
    mcpolis_instance: str | None
    upstream_id: str | None
    org_id: str | None
    created_at: datetime
    age_hours: float
    category: str  # recognized | mine_orphan | foreign_instance | untagged


def _load_persisted_sandbox_ids() -> set[str]:
    """Return the set of ``sandbox_id`` values currently persisted
    in Mongo, if a comma-separated list is supplied via the
    ``MCPOLIS_PERSISTED_SANDBOX_IDS`` env var.

    The CLI deliberately does NOT instantiate the full mcpolis
    Mongo stack — that requires the cloud-mode settings to be set,
    boots the auditing/etc. plumbing, and is heavy for what's
    essentially a reporting tool. The intended usage is:

    - **Routine inspection**: run without the env var. Every
      tagged sandbox falls into ``mine_orphan`` /
      ``foreign_instance`` based on tag, never ``recognized``.
      Operator inspects the table, decides nothing needs killing.
    - **Pre-cleanup**: ``MCPOLIS_PERSISTED_SANDBOX_IDS=$(mongo ...
      'JSON.stringify(...)') bash run-list-orphan-sandboxes.sh
      --delete-orphans``. Pulls the live ref ids out of Mongo with
      whatever tooling the operator prefers, feeds them in as a
      protected-set so this CLI doesn't kill them.

    Returns an empty set when the env var is unset or
    syntactically invalid.
    """
    raw = os.environ.get("MCPOLIS_PERSISTED_SANDBOX_IDS", "")
    if not raw:
        return set()
    return {sid.strip() for sid in raw.split(",") if sid.strip()}


def _classify(
    sandbox: Any,
    persisted_ids: set[str],
    mcpolis_instance: str | None,
    age_min_hours: float,
) -> _Categorised:
    metadata: dict[str, str] = getattr(sandbox, "metadata", {}) or {}
    tag = metadata.get("mcpolis_instance") or None
    upstream_id = metadata.get("mcpolis_upstream") or None
    org_id = metadata.get("mcpolis_org") or None
    created_at: datetime = getattr(sandbox, "created_at", None) or datetime(
        1970, 1, 1, tzinfo=timezone.utc,
    )
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_hours = (
        (datetime.now(tz=timezone.utc) - created_at).total_seconds() / 3600.0
    )
    sid = sandbox.sandbox_id

    if sid in persisted_ids:
        category = "recognized"
    elif tag is None:
        category = "untagged"
    elif mcpolis_instance is not None and tag == mcpolis_instance:
        category = "mine_orphan"
    else:
        # Foreign instance: could be a live peer or a dead one.
        # The age gate gives the operator a safety threshold —
        # below the threshold, we treat it as a possibly-live peer
        # and report it as foreign_instance (read-only); above, the
        # operator may have decided enough time has passed to
        # consider it abandoned. The category itself stays
        # ``foreign_instance``; ``--delete-orphans`` only kills
        # foreign-instance entries when ``age_hours >= age_min_hours``.
        category = "foreign_instance"

    del age_min_hours  # used by the kill loop, not the category itself
    return _Categorised(
        sandbox_id=sid,
        state=getattr(sandbox, "state", "unknown"),
        mcpolis_instance=tag,
        upstream_id=upstream_id,
        org_id=org_id,
        created_at=created_at,
        age_hours=age_hours,
        category=category,
    )


def _print_table(rows: list[_Categorised]) -> None:
    header = (
        "category", "state", "sandbox_id", "age_h",
        "mcpolis_instance", "org/upstream",
    )
    fmt: list[tuple[str, ...]] = [header]
    for r in rows:
        fmt.append((
            r.category,
            r.state,
            r.sandbox_id,
            f"{r.age_hours:.1f}",
            (r.mcpolis_instance or "-")[:12],
            f"{r.org_id or '-'}/{r.upstream_id or '-'}",
        ))
    widths = [max(len(row[i]) for row in fmt) for i in range(len(header))]
    for i, row in enumerate(fmt):
        line = "  ".join(c.ljust(widths[idx]) for idx, c in enumerate(row))
        print(line)
        if i == 0:
            print("  ".join("-" * w for w in widths))


def _print_json(rows: list[_Categorised]) -> None:
    print(json.dumps([
        {
            "category": r.category,
            "state": r.state,
            "sandbox_id": r.sandbox_id,
            "age_hours": round(r.age_hours, 2),
            "mcpolis_instance": r.mcpolis_instance,
            "org_id": r.org_id,
            "upstream_id": r.upstream_id,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ], indent=2))


async def _delete_orphans(
    client: RealE2BClient,
    rows: list[_Categorised],
    age_min_hours: float,
) -> int:
    """Kill every non-``recognized`` sandbox older than the age
    threshold. Returns the kill count."""
    killed = 0
    for r in rows:
        if r.category == "recognized":
            continue
        if r.age_hours < age_min_hours:
            print(
                f"[skip] {r.sandbox_id} ({r.category}): "
                f"age {r.age_hours:.1f}h < {age_min_hours}h threshold",
            )
            continue
        try:
            await client.kill_sandbox(r.sandbox_id)
            print(f"[kill] {r.sandbox_id} ({r.category})")
            killed += 1
        except E2BSDKError as exc:
            print(
                f"[fail] {r.sandbox_id} ({r.category}): {exc}",
                file=sys.stderr,
            )
    return killed


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List + optionally clean up orphan E2B sandboxes "
            "tagged by mcpolis."
        ),
    )
    parser.add_argument(
        "--mcpolis-instance",
        default=os.environ.get("MCPOLIS_INSTANCE_ID"),
        help=(
            "mcpolis instance id to treat as 'mine'. Defaults to "
            "$MCPOLIS_INSTANCE_ID. When unset, every tagged "
            "sandbox falls into ``foreign_instance``."
        ),
    )
    parser.add_argument(
        "--age-min-hours", type=float, default=1.0,
        help=(
            "Minimum sandbox age before --delete-orphans considers "
            "it deletable. Avoids friendly-firing sandboxes a "
            "concurrently-booting instance just created."
        ),
    )
    parser.add_argument(
        "--delete-orphans", action="store_true",
        help="Kill all non-recognized sandboxes older than the threshold.",
    )
    parser.add_argument("--json", action="store_true", help="JSON output.")
    args = parser.parse_args()

    if not API_KEY:
        print(
            "ERROR: MCPOLIS_E2B_API_KEY (or E2B_API_KEY) must be set.",
            file=sys.stderr,
        )
        return 2

    client = RealE2BClient(api_key=API_KEY)
    persisted_ids = _load_persisted_sandbox_ids()

    try:
        sandboxes = await client.list_sandboxes()
    except E2BSDKError as exc:
        print(f"ERROR: list_sandboxes failed: {exc}", file=sys.stderr)
        return 1

    rows = [
        _classify(s, persisted_ids, args.mcpolis_instance, args.age_min_hours)
        for s in sandboxes
    ]
    rows.sort(key=lambda r: (r.category, r.created_at))

    if args.json:
        _print_json(rows)
    else:
        if not rows:
            print("No sandboxes attributed to this E2B account.")
        else:
            _print_table(rows)
            counts: dict[str, int] = {}
            for r in rows:
                counts[r.category] = counts.get(r.category, 0) + 1
            print()
            print(
                "totals: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            )
            print(
                f"persisted refs in Mongo: {len(persisted_ids)}",
            )

    if args.delete_orphans:
        if args.json:
            # Print to stderr so JSON output stays clean.
            print(file=sys.stderr)
        print(
            f"\n--delete-orphans: killing non-recognized sandboxes "
            f"older than {args.age_min_hours}h...",
        )
        killed = await _delete_orphans(client, rows, args.age_min_hours)
        print(f"\nkilled {killed} sandbox(es).")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
