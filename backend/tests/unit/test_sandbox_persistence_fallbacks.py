"""Phase E.1 — strict ``_from_doc`` behavior.

Each test was originally written in Phase E.0 to pin a *fallback* in
``MongoSandboxPersistenceRepository.from_doc``. After E.1's strict
shift, every fallback raises ``MalformedRefError(field=..., reason=...)``
instead. The boundary methods (``get`` / ``list_for_org`` /
``list_all_unscoped``) catch the error, log WARNING, and surface the
ref as missing — the upstream lands in the manager's FAILED state at
first read.

A leading "Was:" comment on each rewrite records the pre-E.1 behavior
so the diff between E.0 and E.1 stays legible at the test layer.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from mcpolis.adapters.repositories.mongo_client import (
    COLL_SANDBOX_REFS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_sandbox_persistence_repository import (
    MongoSandboxPersistenceRepository,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    MalformedRefError,
    SandboxPersistedRef,
)
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


def make_complete_doc(
    *,
    org_id: str = "acme",
    upstream_id: str = "ups-1",
    provider: str = "e2b",
    sandbox_id: str | None = "sbx-abc",
    paused_snapshot_id: str | None = None,
    pid: int | None = 12345,
    metadata: dict[str, str] | None = None,
    instance: str = "instance-1",
    last_updated: datetime | None = None,
    cached_server_info: dict[str, Any] | None = None,
    cached_self_description: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete Mongo-shaped doc — every field present.

    Tests pop / mangle individual fields to exercise each path of
    the strict deserializer.
    """
    return {
        "provider": provider,
        "org_id": org_id,
        "upstream_id": upstream_id,
        "mcpolis_instance": instance,
        "sandbox_id": sandbox_id,
        "paused_snapshot_id": paused_snapshot_id,
        "pid": pid,
        "metadata": (
            metadata if metadata is not None
            else {"template": "mcpolis-node-cpu1-ram1024"}
        ),
        "cached_server_info": cached_server_info,
        "cached_self_description": cached_self_description,
        "last_updated": last_updated or datetime.now(tz=timezone.utc),
    }


# ────────────────────────────────────────────────────────────────────
# _from_doc baseline
# ────────────────────────────────────────────────────────────────────

def test_complete_doc_round_trips() -> None:
    """A complete doc deserializes to a typed ref. (Unchanged from E.0.)"""
    doc = make_complete_doc()
    ref = MongoSandboxPersistenceRepository.from_doc(doc)
    assert isinstance(ref, SandboxPersistedRef)
    assert ref.provider == "e2b"
    assert ref.sandbox_id == "sbx-abc"
    assert ref.pid == 12345
    assert ref.metadata == {"template": "mcpolis-node-cpu1-ram1024"}


def test_legacy_config_hash_field_is_ignored() -> None:
    """Pre-cleanup docs in production carry a stale ``config_hash``
    key. The deserializer must not look at it (the field is gone
    from the model) and must not reject the doc — old docs deploy
    cleanly without a migration."""
    doc = make_complete_doc()
    doc["config_hash"] = "stale-hash-from-old-write"
    ref = MongoSandboxPersistenceRepository.from_doc(doc)
    assert ref.sandbox_id == "sbx-abc"
    assert not hasattr(ref, "config_hash")


def test_unknown_provider_raises_malformed() -> None:
    """Was: raised ``ValueError``. Now: raises ``MalformedRefError`` so the
    boundary catch is type-uniform."""
    doc = make_complete_doc()
    doc["provider"] = "made-up"
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "provider"


# ────────────────────────────────────────────────────────────────────
# Strict shift on missing / wrong-type fields
# ────────────────────────────────────────────────────────────────────

def test_missing_pid_raises_malformed() -> None:
    """Was: missing ``pid`` → ``ref.pid = None``. Now: raises
    ``MalformedRefError(field='pid', reason='missing')``."""
    doc = make_complete_doc()
    del doc["pid"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "pid"
    assert exc_info.value.reason == "missing"


def test_pid_non_int_raises_malformed() -> None:
    """Was: ``pid='not-an-int'`` → ``ref.pid = None``. Now: raises."""
    doc = make_complete_doc()
    doc["pid"] = "not-an-int"
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "pid"


def test_pid_bool_rejected() -> None:
    """``bool`` is a subclass of ``int`` in Python; an explicit guard
    prevents a stray ``True`` from passing as a pid."""
    doc = make_complete_doc()
    doc["pid"] = True
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "pid"


def test_pid_explicit_none_accepted() -> None:
    """``None`` is a valid value for a ref that has no live process
    (paused-only). The strict deserializer accepts an explicit None
    as long as the key is present."""
    doc = make_complete_doc(pid=None)
    ref = MongoSandboxPersistenceRepository.from_doc(doc)
    assert ref.pid is None


def test_missing_sandbox_id_raises_malformed() -> None:
    doc = make_complete_doc()
    del doc["sandbox_id"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "sandbox_id"


def test_sandbox_id_non_str_raises_malformed() -> None:
    doc = make_complete_doc()
    doc["sandbox_id"] = 12345
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "sandbox_id"


def test_missing_paused_snapshot_id_raises_malformed() -> None:
    doc = make_complete_doc(paused_snapshot_id="snap-1", sandbox_id=None)
    del doc["paused_snapshot_id"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "paused_snapshot_id"


def test_missing_cached_server_info_raises_malformed() -> None:
    doc = make_complete_doc()
    del doc["cached_server_info"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "cached_server_info"


def test_cached_server_info_invalid_dict_raises_malformed() -> None:
    """Was: validation failure → ``None`` + WARNING log. Now: raises
    ``MalformedRefError`` so the boundary catches and the upstream
    lands in FAILED rather than booting with a half-deserialized ref."""
    doc = make_complete_doc()
    doc["cached_server_info"] = {"not": "valid"}
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "cached_server_info"


def test_missing_cached_self_description_raises_malformed() -> None:
    doc = make_complete_doc()
    del doc["cached_self_description"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "cached_self_description"


# ────────────────────────────────────────────────────────────────────
# metadata strictness
# ────────────────────────────────────────────────────────────────────

def test_missing_metadata_raises_malformed() -> None:
    """Was: missing ``metadata`` → ``ref.metadata = {}``. Now: raises."""
    doc = make_complete_doc()
    del doc["metadata"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "metadata"


def test_metadata_non_dict_raises_malformed() -> None:
    """Was: non-dict → ``ref.metadata = {}`` silently. Now: raises."""
    doc = make_complete_doc()
    doc["metadata"] = "not-a-dict"
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "metadata"


def test_metadata_non_string_kv_raises_malformed() -> None:
    """Was: non-string keys/values silently dropped. Now: raises with
    the specific subkey identified so the operator can find the
    offending row."""
    doc = make_complete_doc()
    doc["metadata"] = {"valid": "ok", "numeric_value": 1}
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    # Subkey identification — the metadata field that broke is named.
    assert exc_info.value.field.startswith("metadata")


def test_metadata_empty_dict_accepted() -> None:
    """An empty dict is valid metadata; only *missing* the field
    is malformed."""
    doc = make_complete_doc(metadata={})
    ref = MongoSandboxPersistenceRepository.from_doc(doc)
    assert ref.metadata == {}


# ────────────────────────────────────────────────────────────────────
# last_updated strictness
# ────────────────────────────────────────────────────────────────────

def test_missing_last_updated_raises_malformed() -> None:
    """Was: missing ``last_updated`` → ``ref.last_updated = now()``,
    making a corrupt doc look fresh to the reconciler. Now: raises."""
    doc = make_complete_doc()
    del doc["last_updated"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "last_updated"


def test_invalid_last_updated_raises_malformed() -> None:
    """Was: list / non-datetime / non-str → ``ref.last_updated = now()``.
    Now: raises."""
    doc = make_complete_doc()
    doc["last_updated"] = ["not-a-datetime"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "last_updated"


def test_last_updated_iso_string_parses() -> None:
    """ISO string remains valid input — the writer can persist either
    a datetime or a serialized string and the deserializer accepts
    both. (Unchanged from E.0.)"""
    doc = make_complete_doc()
    doc["last_updated"] = "2026-01-15T12:30:00+00:00"
    ref = MongoSandboxPersistenceRepository.from_doc(doc)
    assert ref.last_updated == datetime(
        2026, 1, 15, 12, 30, tzinfo=timezone.utc,
    )


# ────────────────────────────────────────────────────────────────────
# mcpolis_instance — fallback was always invalid (Field(min_length=1))
# ────────────────────────────────────────────────────────────────────

def test_missing_mcpolis_instance_raises_malformed() -> None:
    """Was: fell back to ``""`` and tripped pydantic's ``min_length=1``,
    raising ``ValidationError``. Now: raises ``MalformedRefError`` at
    the field level — same outcome, named field."""
    doc = make_complete_doc()
    del doc["mcpolis_instance"]
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "mcpolis_instance"


def test_empty_mcpolis_instance_raises_malformed() -> None:
    doc = make_complete_doc(instance="")
    with pytest.raises(MalformedRefError) as exc_info:
        MongoSandboxPersistenceRepository.from_doc(doc)
    assert exc_info.value.field == "mcpolis_instance"


# ────────────────────────────────────────────────────────────────────
# Migration-shape tests — what the script must drop vs. upgrade
# ────────────────────────────────────────────────────────────────────
#
# After Phase E.1 the migration script is the only path that brings
# legacy docs into the strict shape. These tests pin the contract the
# script must honor:
#
#   * Complete-shape doc → loads cleanly; the script's "upgrade in
#     place" path is a no-op (it touches nothing).
#   * Doc missing any required field → MUST be dropped (or upgraded
#     by the script) before a fresh deploy reads it; otherwise the
#     boundary catch sees a WARNING storm.
#
# The companion migration-script test (``test_sandbox_refs_migration.py``)
# pins the *script's* behavior. These tests pin the *deserializer's*
# end of the contract.

def test_legacy_pre_reconnect_feature_doc_is_now_malformed() -> None:
    """Was (E.0): older refs lacking ``pid``/cached_* loaded
    successfully because each field had a None fallback. Now:
    every missing field is rejected. Pre-reconnect-feature refs MUST
    be upgraded by the migration script (each missing field set to
    explicit ``None``) or dropped before this code reads them."""
    doc: dict[str, Any] = {
        "provider": "e2b",
        "org_id": "acme",
        "upstream_id": "ups-legacy",
        "mcpolis_instance": "instance-old",
        "sandbox_id": "sbx-legacy",
        "metadata": {},
        "last_updated": datetime.now(tz=timezone.utc),
        # Deliberately absent: pid, paused_snapshot_id,
        # cached_server_info, cached_self_description.
    }
    with pytest.raises(MalformedRefError):
        MongoSandboxPersistenceRepository.from_doc(doc)


def test_complete_doc_with_explicit_nulls_loads() -> None:
    """The migration-script's "upgrade in place" mode writes explicit
    ``None`` for the previously-missing nullable fields. This test
    pins that the upgraded shape is acceptable to the strict
    deserializer — the script's output is well-formed.

    A stray ``config_hash`` key (carried over from prod docs written
    before the field was removed from the model) is still tolerated
    — the deserializer ignores it without raising."""
    upgraded: dict[str, Any] = {
        "provider": "e2b",
        "org_id": "acme",
        "upstream_id": "ups-upgraded",
        "mcpolis_instance": "instance-old",
        "sandbox_id": "sbx-legacy",
        "paused_snapshot_id": None,
        "pid": None,
        "config_hash": None,
        "metadata": {},
        "cached_server_info": None,
        "cached_self_description": None,
        "last_updated": datetime.now(tz=timezone.utc),
    }
    ref = MongoSandboxPersistenceRepository.from_doc(upgraded)
    assert ref.upstream_id == "ups-upgraded"
    assert ref.pid is None
    assert not hasattr(ref, "config_hash")


# ────────────────────────────────────────────────────────────────────
# Boundary behavior — list_for_org skip + WARNING; get returns None
# ────────────────────────────────────────────────────────────────────
#
# Was (E.0): list_for_org caught ``Exception``; get propagated.
# Now: both catch ``MalformedRefError`` specifically. Other
# exceptions still propagate (a true bug should not be swallowed).

@asynccontextmanager
async def _make_mongo_repo() -> AsyncIterator[
    tuple[MongoSandboxPersistenceRepository, Any],
]:
    async with temp_mongo_database() as db:
        raw = db[COLL_SANDBOX_REFS]
        coll = OrgScopedCollection(raw, COLL_SANDBOX_REFS)
        yield MongoSandboxPersistenceRepository(coll), raw


@pytest.mark.skipif(
    not mongo_available(),
    reason="Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)",
)
@pytest.mark.asyncio
async def test_list_for_org_skips_malformed_doc() -> None:
    """A malformed doc in a list is logged WARNING (with ``field`` /
    ``reason``) and skipped; the rest of the list returns. Without
    this, one bad ref would blank the entire boot reconciler's
    view."""
    async with _make_mongo_repo() as (repo, raw):
        good = make_complete_doc(upstream_id="good")
        bad = make_complete_doc(upstream_id="bad")
        del bad["mcpolis_instance"]
        await raw.insert_one(good)
        await raw.insert_one(bad)

        loaded = await repo.list_for_org(org_id="acme")
        ids = {r.upstream_id for r in loaded}
        assert ids == {"good"}


@pytest.mark.skipif(
    not mongo_available(),
    reason="Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)",
)
@pytest.mark.asyncio
async def test_get_returns_none_on_malformed_doc() -> None:
    """Was (E.0): ``get`` propagated the ``ValidationError`` from the
    bad fallback, leaking implementation details to callers. Now:
    catches ``MalformedRefError``, logs WARNING, returns ``None`` —
    the manager treats this as "no ref" and lands the upstream in
    FAILED state at first read."""
    async with _make_mongo_repo() as (repo, raw):
        bad = make_complete_doc(upstream_id="bad")
        del bad["mcpolis_instance"]
        await raw.insert_one(bad)
        result = await repo.get(org_id="acme", upstream_id="bad")
        assert result is None
