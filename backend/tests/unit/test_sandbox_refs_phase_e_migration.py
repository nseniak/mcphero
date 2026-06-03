"""Unit tests for the Phase E sandbox_refs migration classifier.

The classifier is a pure function: it inspects a raw Mongo doc and
returns a ``Plan`` describing the action (unchanged / upgrade /
drop). These tests pin every branch so the script's prod behavior
is predictable from CI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcpolis.adapters.repositories.migrations.sandbox_refs_phase_e import (
    classify_doc,
)


def make_complete_doc(
    *,
    org_id: str = "acme",
    upstream_id: str = "ups-1",
) -> dict[str, Any]:
    return {
        "provider": "e2b",
        "org_id": org_id,
        "upstream_id": upstream_id,
        "mcpolis_instance": "instance-1",
        "sandbox_id": "sbx-abc",
        "paused_snapshot_id": None,
        "pid": 12345,
        "metadata": {},
        "cached_server_info": None,
        "cached_self_description": None,
        "last_updated": datetime.now(tz=timezone.utc),
    }


def test_complete_doc_is_unchanged() -> None:
    """A doc that already passes the strict deserializer is left
    untouched — the script writes nothing for it."""
    plan = classify_doc(make_complete_doc())
    assert plan.kind == "unchanged"
    assert plan.upgrade_to is None
    assert plan.reason is None


def test_missing_pid_is_upgraded() -> None:
    """The pre-reconnect-feature shape (no ``pid``) is the
    load-bearing legacy case. The script fills in explicit ``None``
    so the doc passes the strict deserializer."""
    doc = make_complete_doc()
    del doc["pid"]
    plan = classify_doc(doc)
    assert plan.kind == "upgraded"
    assert plan.upgrade_to is not None
    assert plan.upgrade_to["pid"] is None


def test_missing_multiple_nullable_fields_is_upgraded_in_one_pass() -> None:
    """The classifier's bounded-fixpoint loop fills every missing
    nullable field, not just the first one. Pre-reconnect refs
    typically lack three fields at once; one pass must cover all."""
    doc = make_complete_doc()
    for f in ("pid", "cached_server_info", "cached_self_description"):
        del doc[f]
    plan = classify_doc(doc)
    assert plan.kind == "upgraded"
    assert plan.upgrade_to is not None
    assert plan.upgrade_to["pid"] is None
    assert plan.upgrade_to["cached_server_info"] is None
    assert plan.upgrade_to["cached_self_description"] is None


def test_missing_metadata_is_upgraded_to_empty_dict() -> None:
    doc = make_complete_doc()
    del doc["metadata"]
    plan = classify_doc(doc)
    assert plan.kind == "upgraded"
    assert plan.upgrade_to is not None
    assert plan.upgrade_to["metadata"] == {}


def test_missing_required_field_is_dropped() -> None:
    """``mcpolis_instance`` has no safe default; the script drops
    the doc rather than fabricate an instance id."""
    doc = make_complete_doc()
    del doc["mcpolis_instance"]
    plan = classify_doc(doc)
    assert plan.kind == "dropped"
    assert plan.reason is not None
    assert "mcpolis_instance" in plan.reason


def test_missing_last_updated_is_dropped() -> None:
    """``last_updated`` is the field whose pre-E.1 fallback was most
    actively harmful (a corrupt doc looked fresh). The script
    refuses to fabricate it."""
    doc = make_complete_doc()
    del doc["last_updated"]
    plan = classify_doc(doc)
    assert plan.kind == "dropped"
    assert plan.reason is not None
    assert "last_updated" in plan.reason


def test_wrong_typed_field_is_dropped() -> None:
    """A non-recoverable type mismatch (not just missing) → drop.
    The script doesn't try to coerce types — that's a sharper
    edge than this migration is willing to walk."""
    doc = make_complete_doc()
    doc["pid"] = "not-an-int"
    plan = classify_doc(doc)
    assert plan.kind == "dropped"


def test_unknown_provider_is_dropped() -> None:
    doc = make_complete_doc()
    doc["provider"] = "made-up-runner"
    plan = classify_doc(doc)
    assert plan.kind == "dropped"
    assert plan.reason is not None
    assert "provider" in plan.reason


def test_upgrade_preserves_real_field_values() -> None:
    """The script must not overwrite values it doesn't recognize as
    needing migration. ``sandbox_id`` is preserved through an
    upgrade triggered by a different missing field."""
    doc = make_complete_doc()
    del doc["pid"]
    plan = classify_doc(doc)
    assert plan.kind == "upgraded"
    assert plan.upgrade_to is not None
    assert plan.upgrade_to["sandbox_id"] == "sbx-abc"
    assert plan.upgrade_to["mcpolis_instance"] == "instance-1"
