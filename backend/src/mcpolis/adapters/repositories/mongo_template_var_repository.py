"""Mongo-backed env-var store with field-level AES-256-GCM at rest.

Only the ``value`` field is encrypted (via the existing
:class:`mcpolis.adapters.repositories.encryption.FieldEncryptor`,
declared in ``ENCRYPTED_FIELDS[COLL_TEMPLATE_VARS]`` so the
:class:`OrgScopedCollection` wrapper applies it transparently). The
``is_secret`` flag, ``last_four`` preview, and timestamps stay
plaintext so the listing path never needs to decrypt.

Encryption is **uniform** — every row's value is encrypted at rest
regardless of ``is_secret``. Cheap defence in depth: the field-level
encryption is a collection-level invariant in
:mod:`mcpolis.adapters.repositories.mongo_client`'s
``ENCRYPTED_FIELDS``, and per-doc branching there would complicate
the wrapper for marginal benefit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.model.template_var import (
    TemplateVarSummary,
    compute_last_four,
)
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository


def _summary_from_doc(doc: dict[str, Any]) -> TemplateVarSummary:
    # Default ``is_secret=True`` for back-compat with v1 docs that
    # pre-date the field. The list path now returns ``value`` for both
    # kinds — the UI obfuscates password rows by default and exposes an
    # eye toggle (1Password-style). Encryption-at-rest still applies
    # uniformly via ``ENCRYPTED_FIELDS``; the value is decrypted by
    # the OrgScopedCollection wrapper before this helper sees it.
    is_secret = bool(doc.get("is_secret", True))
    stored_value = doc.get("value")
    value: str | None = (
        stored_value if isinstance(stored_value, str) else None
    )
    return TemplateVarSummary(
        name=doc["name"],
        is_secret=is_secret,
        value=value,
        last_four=doc.get("last_four"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


class MongoTemplateVarRepository(TemplateVarRepository):
    def __init__(self, template_vars: OrgScopedCollection) -> None:
        self._template_vars = template_vars

    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[TemplateVarSummary]:
        docs = await self._template_vars.find_many(
            org_id,
            {"upstream_id": upstream_id},
            sort=[("name", 1)],
        )
        return [_summary_from_doc(d) for d in docs]

    async def get_value(
        self, org_id: str, upstream_id: str, name: str
    ) -> str | None:
        doc = await self._template_vars.find_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )
        if doc is None:
            return None
        value = doc.get("value")
        return value if isinstance(value, str) else None

    async def set(
        self,
        org_id: str,
        upstream_id: str,
        name: str,
        value: str,
        *,
        is_secret: bool = True,
    ) -> TemplateVarSummary:
        now = datetime.now(tz=timezone.utc)
        existing = await self._template_vars.find_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )
        created_at: datetime = (
            existing["created_at"] if existing is not None else now
        )
        # Replace contract: existing row's ``is_secret`` wins. See
        # :class:`TemplateVarRepository.set` docstring for rationale.
        effective_is_secret: bool = (
            bool(existing.get("is_secret", True))
            if existing is not None
            else is_secret
        )
        last_four = compute_last_four(value)
        await self._template_vars.replace_one(
            org_id,
            {"upstream_id": upstream_id, "name": name},
            {
                "upstream_id": upstream_id,
                "name": name,
                "value": value,
                "is_secret": effective_is_secret,
                "last_four": last_four,
                "created_at": created_at,
                "updated_at": now,
            },
            upsert=True,
        )
        return TemplateVarSummary(
            name=name,
            is_secret=effective_is_secret,
            value=value,
            last_four=last_four,
            created_at=created_at,
            updated_at=now,
        )

    async def delete(
        self, org_id: str, upstream_id: str, name: str
    ) -> None:
        await self._template_vars.delete_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )

    async def delete_all(
        self, org_id: str, upstream_id: str
    ) -> None:
        await self._template_vars.delete_many(
            org_id, {"upstream_id": upstream_id},
        )
