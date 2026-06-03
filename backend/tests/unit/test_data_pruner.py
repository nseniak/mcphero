"""Tests for ``data_pruner`` — specifically the ``__admin__`` safety net.

``prune_data`` walks ``connections.json`` and removes per-user entries
whose ``user_id`` isn't in the configured user roster. The ``__admin__``
sentinel is NOT a real user and will never appear in ``valid_emails``,
so the pruner has to explicitly exempt it — otherwise every startup
would delete the admin's OAuth tokens and force re-authentication.

Pinning this invariant matters because:

  * It's the only thing standing between a deploy and mass admin
    session loss. Nothing else in the system treats ``__admin__`` as
    protected at the persistence layer.
  * A plausible refactor of the pruner iteration model (e.g., "scope
    to real emails up front") would silently drop the check. The
    audit log won't notice — the pruner writes the file directly,
    no domain event is emitted.
"""
from __future__ import annotations

import json
from pathlib import Path

from mcpolis.domain.services.data_pruner import prune_data


def _write_connections(path: Path, rows: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def test_prune_data_never_deletes_admin_tokens(tmp_path: Path) -> None:
    """Admin tokens must survive even when ``__admin__`` isn't in
    ``valid_emails`` (it never is — it's a sentinel, not a real email)."""
    conn_path = tmp_path / "connections.json"
    _write_connections(conn_path, {
        "user:notion:__admin__": {"token": {"access_token": "admin-at"}},
        "user:notion:alice@co.com": {"token": {"access_token": "alice-at"}},
        "user:notion:orphan@co.com": {"token": {"access_token": "orphan-at"}},
    })

    prune_data(
        org_id="default",
        data_dir=tmp_path,
        valid_emails={"alice@co.com"},
        valid_upstream_ids={"notion"},
    )

    remaining = json.loads(conn_path.read_text())
    # Admin row preserved, alice preserved, orphan pruned.
    assert "user:notion:__admin__" in remaining
    assert "user:notion:alice@co.com" in remaining
    assert "user:notion:orphan@co.com" not in remaining


def test_prune_data_preserves_admin_client_info_and_pending_code(
    tmp_path: Path,
) -> None:
    """The exemption must cover every per-user key prefix — not just
    ``user:``. A narrower check would leak client-info or pending-code
    records past the admin-protection barrier on the next prune."""
    conn_path = tmp_path / "connections.json"
    _write_connections(conn_path, {
        "user:notion:__admin__": {"token": {"access_token": "at"}},
        "client_info:notion:__admin__": {"client_id": "cid"},
        "pending_code:notion:__admin__": {"code": "xyz"},
    })

    prune_data(
        org_id="default",
        data_dir=tmp_path,
        valid_emails=set(),  # No real users at all
        valid_upstream_ids={"notion"},
    )

    remaining = json.loads(conn_path.read_text())
    assert "user:notion:__admin__" in remaining
    assert "client_info:notion:__admin__" in remaining
    assert "pending_code:notion:__admin__" in remaining


def test_prune_data_still_drops_admin_rows_for_removed_upstreams(
    tmp_path: Path,
) -> None:
    """The admin exemption is scoped to the user axis only. If the
    upstream itself is removed from config, every row for that
    upstream (admin included) should go. Otherwise removed upstreams
    would leave zombie tokens that the pruner can never reach again."""
    conn_path = tmp_path / "connections.json"
    _write_connections(conn_path, {
        "user:removed-mcp:__admin__": {"token": {"access_token": "at"}},
        "user:notion:__admin__": {"token": {"access_token": "at"}},
    })

    prune_data(
        org_id="default",
        data_dir=tmp_path,
        valid_emails=set(),
        valid_upstream_ids={"notion"},  # "removed-mcp" is gone
    )

    remaining = json.loads(conn_path.read_text())
    assert "user:removed-mcp:__admin__" not in remaining
    assert "user:notion:__admin__" in remaining
