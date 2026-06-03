"""Mongo-backed ``SandboxPersistenceRepository``.

One document per ``(org_id, upstream_id)`` in the ``sandbox_refs``
collection. ``OrgScopedCollection`` injects ``org_id`` on every
read/write. No fields are encrypted: the IDs are opaque tokens
attributed back to mcpolis via the ``mcpolis_instance`` metadata tag,
not credentials.

Phase E note: ``from_doc`` is **strict** — every field of
``SandboxPersistedRef`` must be present in the doc with a value of
the expected type or an explicit ``None`` where the domain allows.
Deviations raise ``MalformedRefError``; the boundary methods
(``get`` / ``list_for_org`` / ``list_all_unscoped``) catch and log
WARNING with ``field`` + ``reason``. Callers see the doc as missing,
which lands the upstream in the manager's FAILED state. The
companion migration script (see ``adapters/repositories/migrations/``)
upgrades or drops legacy docs at deploy time so the steady-state
warning count is zero.

The cross-org sweep used by per-backend reconcilers calls
:meth:`list_all_unscoped` which goes through
``find_many_cross_org`` — the deliberate operator-only escape hatch.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from pydantic import ValidationError

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.model.upstream import (
    ServerInfo,
    UpstreamSelfDescription,
)
from mcpolis.domain.ports.sandbox_persistence_repository import (
    MalformedRefError,
    SandboxPersistedRef,
    SandboxPersistenceRepository,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class MongoSandboxPersistenceRepository(SandboxPersistenceRepository):
    def __init__(self, coll: OrgScopedCollection) -> None:
        self._coll = coll

    async def upsert(self, ref: SandboxPersistedRef) -> None:
        await self._coll.replace_one(
            ref.org_id,
            {"upstream_id": ref.upstream_id},
            self._to_doc(ref),
            upsert=True,
        )

    async def get(
        self, *, org_id: str, upstream_id: str,
    ) -> SandboxPersistedRef | None:
        doc = await self._coll.find_one(
            org_id, {"upstream_id": upstream_id},
        )
        if doc is None:
            return None
        try:
            return self.from_doc(doc)
        except MalformedRefError as exc:
            logger.warning(
                "sandbox_persistence.deserialize.malformed",
                org_id=org_id,
                upstream_id=upstream_id,
                field=exc.field,
                reason=exc.reason,
            )
            return None

    async def delete(self, *, org_id: str, upstream_id: str) -> None:
        await self._coll.delete_one(
            org_id, {"upstream_id": upstream_id},
        )

    async def list_for_org(
        self, *, org_id: str,
    ) -> list[SandboxPersistedRef]:
        docs = await self._coll.find_many(org_id)
        out: list[SandboxPersistedRef] = []
        for doc in docs:
            try:
                out.append(self.from_doc(doc))
            except MalformedRefError as exc:
                logger.warning(
                    "sandbox_persistence.deserialize.malformed",
                    org_id=org_id,
                    upstream_id=doc.get("upstream_id"),
                    field=exc.field,
                    reason=exc.reason,
                )
        return out

    async def list_all_unscoped(self) -> list[SandboxPersistedRef]:
        docs = await self._coll.find_many_cross_org()
        out: list[SandboxPersistedRef] = []
        for doc in docs:
            try:
                out.append(self.from_doc(doc))
            except MalformedRefError as exc:
                logger.warning(
                    "sandbox_persistence.deserialize.malformed",
                    org_id=doc.get("org_id"),
                    upstream_id=doc.get("upstream_id"),
                    field=exc.field,
                    reason=exc.reason,
                )
        return out

    @staticmethod
    def _to_doc(ref: SandboxPersistedRef) -> dict[str, Any]:
        return {
            "provider": ref.provider,
            "upstream_id": ref.upstream_id,
            "mcpolis_instance": ref.mcpolis_instance,
            "sandbox_id": ref.sandbox_id,
            "paused_snapshot_id": ref.paused_snapshot_id,
            "pid": ref.pid,
            "metadata": dict(ref.metadata),
            "cached_server_info": (
                ref.cached_server_info.model_dump(mode="json")
                if ref.cached_server_info is not None else None
            ),
            "cached_self_description": (
                ref.cached_self_description.model_dump(mode="json")
                if ref.cached_self_description is not None else None
            ),
            "last_updated": ref.last_updated,
        }

    @staticmethod
    def from_doc(doc: dict[str, Any]) -> SandboxPersistedRef:
        """Strict deserializer — every field must be present with the
        expected type (or an explicit ``None`` where domain allows).
        Public so the Phase E migration script can reuse it as the
        canonical "is this doc well-shaped?" check; otherwise
        callers reach this through the boundary methods which catch
        ``MalformedRefError`` and treat the ref as missing.
        """
        provider = doc.get("provider")
        if provider not in {"own-runner", "e2b", "local-subprocess"}:
            raise MalformedRefError(
                field="provider", reason=f"unknown {provider!r}",
            )

        org_id = _required_str(doc, "org_id")
        upstream_id = _required_str(doc, "upstream_id")
        mcpolis_instance = _required_str(doc, "mcpolis_instance")
        sandbox_id = _nullable_str(doc, "sandbox_id")
        paused_snapshot_id = _nullable_str(doc, "paused_snapshot_id")
        pid = _nullable_int(doc, "pid")
        metadata = _required_str_dict(doc, "metadata")
        cached_server_info = _nullable_model(
            doc, "cached_server_info", ServerInfo,
        )
        cached_self_description = _nullable_model(
            doc, "cached_self_description", UpstreamSelfDescription,
        )
        last_updated = _required_datetime(doc, "last_updated")

        try:
            return SandboxPersistedRef(
                provider=provider,
                org_id=org_id,
                upstream_id=upstream_id,
                mcpolis_instance=mcpolis_instance,
                sandbox_id=sandbox_id,
                paused_snapshot_id=paused_snapshot_id,
                pid=pid,
                metadata=metadata,
                cached_server_info=cached_server_info,
                cached_self_description=cached_self_description,
                last_updated=last_updated,
            )
        except ValidationError as exc:
            # Per-field helpers should already cover the model's
            # constraints; if pydantic still rejects we surface the
            # error message rather than swallow it. (No `field` —
            # use a sentinel so the WARNING line still has a value.)
            raise MalformedRefError(
                field="<model>", reason=str(exc),
            ) from exc


# ── per-field strict parsers ────────────────────────────────────────

def _required_str(doc: dict[str, Any], field: str) -> str:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    value = doc[field]
    if not isinstance(value, str):
        raise MalformedRefError(
            field=field,
            reason=f"expected str, got {type(value).__name__}",
        )
    if not value:
        raise MalformedRefError(field=field, reason="empty string")
    return value


def _nullable_str(doc: dict[str, Any], field: str) -> str | None:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    value = doc[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedRefError(
            field=field,
            reason=f"expected str | None, got {type(value).__name__}",
        )
    return value


def _nullable_int(doc: dict[str, Any], field: str) -> int | None:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    value = doc[field]
    if value is None:
        return None
    # ``bool`` is a subclass of ``int`` in Python — exclude it
    # explicitly so a stray boolean doesn't pass as a pid.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedRefError(
            field=field,
            reason=f"expected int | None, got {type(value).__name__}",
        )
    return value


def _required_str_dict(doc: dict[str, Any], field: str) -> dict[str, str]:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    raw = doc[field]
    if not isinstance(raw, dict):
        raise MalformedRefError(
            field=field,
            reason=f"expected dict, got {type(raw).__name__}",
        )
    raw_items: dict[Any, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    out: dict[str, str] = {}
    for k, v in raw_items.items():
        k_typed: Any = k
        v_typed: Any = v
        if not isinstance(k_typed, str):
            raise MalformedRefError(
                field=field,
                reason=f"non-str key: {type(k_typed).__name__}",
            )
        if not isinstance(v_typed, str):
            raise MalformedRefError(
                field=f"{field}[{k_typed}]",
                reason=f"non-str value: {type(v_typed).__name__}",
            )
        out[k_typed] = v_typed
    return out


def _nullable_model[T](
    doc: dict[str, Any], field: str, model: type[T],
) -> T | None:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    raw = doc[field]
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MalformedRefError(
            field=field,
            reason=f"expected dict | None, got {type(raw).__name__}",
        )
    try:
        return model.model_validate(raw)  # type: ignore[attr-defined]
    except ValidationError as exc:
        raise MalformedRefError(
            field=field, reason=f"validation failed: {exc}",
        ) from exc


def _required_datetime(doc: dict[str, Any], field: str) -> datetime:
    if field not in doc:
        raise MalformedRefError(field=field, reason="missing")
    raw = doc[field]
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise MalformedRefError(
                field=field, reason=f"invalid ISO: {exc}",
            ) from exc
    raise MalformedRefError(
        field=field,
        reason=f"expected datetime or str, got {type(raw).__name__}",
    )


__all__ = ["MongoSandboxPersistenceRepository"]
