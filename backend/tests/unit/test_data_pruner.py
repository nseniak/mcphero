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


def test_prune_data_drops_every_key_prefix_for_removed_upstream(
    tmp_path: Path,
) -> None:
    """The pruner must reach EVERY per-upstream key prefix, not a stale
    subset. A prefix it doesn't know about leaks past every future prune
    — the exact class of bug behind the ``oauth_metadata`` /
    ``client_info`` orphans that re-brick a re-added upstream."""
    conn_path = tmp_path / "connections.json"
    _write_connections(conn_path, {
        # Removed upstream "gone": every shape must be swept.
        "user:gone:alice@co.com": {"token": {"access_token": "at"}},
        "client_info:gone:alice@co.com": {"client_id": "cid"},
        "oauth_metadata:gone:alice@co.com": {"issuer": "iss"},
        "pending_code:gone:alice@co.com": {"code": "c"},
        "failures:gone:alice@co.com": {"count": 1},
        "notified:gone:alice@co.com": {"notified_at": "t"},
        "admin:gone": {"token": {"access_token": "at"}},
        "error:gone": {"error": "boom"},
        "enabled:gone": False,
        "started_config_hash:gone": "h",
        # Surviving upstream "notion" / valid user must remain.
        "oauth_metadata:notion:alice@co.com": {"issuer": "iss"},
        "started_config_hash:notion": "h",
    })

    prune_data(
        org_id="default",
        data_dir=tmp_path,
        valid_emails={"alice@co.com"},
        valid_upstream_ids={"notion"},
    )

    remaining = json.loads(conn_path.read_text())
    for gone_key in (
        "user:gone:alice@co.com",
        "client_info:gone:alice@co.com",
        "oauth_metadata:gone:alice@co.com",
        "pending_code:gone:alice@co.com",
        "failures:gone:alice@co.com",
        "notified:gone:alice@co.com",
        "admin:gone",
        "error:gone",
        "enabled:gone",
        "started_config_hash:gone",
    ):
        assert gone_key not in remaining, gone_key
    assert "oauth_metadata:notion:alice@co.com" in remaining
    assert "started_config_hash:notion" in remaining


def test_prune_data_drops_user_axis_prefixes_for_orphan_user(
    tmp_path: Path,
) -> None:
    """The user-axis sweep must cover ``oauth_metadata`` / ``failures`` /
    ``notified`` too — an orphaned user (removed from the roster) on a
    still-valid upstream must leave none of their per-user rows behind,
    or a re-invite inherits stale state."""
    conn_path = tmp_path / "connections.json"
    _write_connections(conn_path, {
        "oauth_metadata:notion:orphan@co.com": {"issuer": "iss"},
        "failures:notion:orphan@co.com": {"count": 2},
        "notified:notion:orphan@co.com": {"notified_at": "t"},
        # Valid user's rows must survive.
        "oauth_metadata:notion:alice@co.com": {"issuer": "iss"},
    })

    prune_data(
        org_id="default",
        data_dir=tmp_path,
        valid_emails={"alice@co.com"},
        valid_upstream_ids={"notion"},
    )

    remaining = json.loads(conn_path.read_text())
    assert "oauth_metadata:notion:orphan@co.com" not in remaining
    assert "failures:notion:orphan@co.com" not in remaining
    assert "notified:notion:orphan@co.com" not in remaining
    assert "oauth_metadata:notion:alice@co.com" in remaining


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
