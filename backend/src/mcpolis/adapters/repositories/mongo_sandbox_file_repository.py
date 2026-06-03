"""Mongo-backed sandbox-file store with field-level AES-256-GCM at rest.

Only the ``contents`` field is encrypted (via the existing
:class:`mcpolis.adapters.repositories.encryption.FieldEncryptor`,
declared in ``ENCRYPTED_FIELDS[COLL_SANDBOX_FILES]`` so the
:class:`OrgScopedCollection` wrapper applies it transparently).
``sha256``, ``size_bytes``, ``target_path``, and timestamps stay
plaintext so the listing path never needs to decrypt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.domain.model.sandbox_file import (
    SandboxFile,
    SandboxFileSummary,
    compute_sha256,
)
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository


def _resolve_display_name(doc: dict[str, Any]) -> str:
    """Legacy rows that pre-date ``display_name`` fall back to ``name``
    so the listing always renders something readable."""
    raw = doc.get("display_name")
    if isinstance(raw, str) and raw.strip():
        return raw
    return str(doc["name"])


def _summary_from_doc(doc: dict[str, Any]) -> SandboxFileSummary:
    return SandboxFileSummary(
        name=doc["name"],
        display_name=_resolve_display_name(doc),
        target_path=str(doc.get("target_path", "")),
        sha256=str(doc.get("sha256", "")),
        size_bytes=int(doc.get("size_bytes", 0)),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _full_from_doc(doc: dict[str, Any]) -> SandboxFile:
    contents = doc.get("contents")
    if not isinstance(contents, str):
        contents = ""
    return SandboxFile(
        name=doc["name"],
        display_name=_resolve_display_name(doc),
        contents=contents,
        target_path=str(doc.get("target_path", "")),
        sha256=str(doc.get("sha256", "")),
        size_bytes=int(doc.get("size_bytes", 0)),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


class MongoSandboxFileRepository(SandboxFileRepository):
    def __init__(self, sandbox_files: OrgScopedCollection) -> None:
        self._sandbox_files = sandbox_files

    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFileSummary]:
        docs = await self._sandbox_files.find_many(
            org_id,
            {"upstream_id": upstream_id},
            sort=[("name", 1)],
        )
        return [_summary_from_doc(d) for d in docs]

    async def list_full(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFile]:
        docs = await self._sandbox_files.find_many(
            org_id,
            {"upstream_id": upstream_id},
            sort=[("name", 1)],
        )
        return [_full_from_doc(d) for d in docs]

    async def get(
        self, org_id: str, upstream_id: str, name: str
    ) -> SandboxFile | None:
        doc = await self._sandbox_files.find_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )
        if doc is None:
            return None
        return _full_from_doc(doc)

    async def set(
        self,
        org_id: str,
        upstream_id: str,
        name: str,
        contents: str,
        target_path: str,
        display_name: str | None = None,
    ) -> SandboxFileSummary:
        now = datetime.now(tz=timezone.utc)
        sha256 = compute_sha256(contents)
        size_bytes = len(contents.encode("utf-8"))
        # Default ``display_name`` to ``name`` when the caller didn't
        # supply one — same fallback the file repo uses, mirrored here
        # so cloud-mode rows match standalone-mode rows.
        resolved_display_name = (
            display_name.strip() if display_name and display_name.strip()
            else name
        )
        existing = await self._sandbox_files.find_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )
        created_at: datetime = (
            existing["created_at"] if existing is not None else now
        )
        await self._sandbox_files.replace_one(
            org_id,
            {"upstream_id": upstream_id, "name": name},
            {
                "upstream_id": upstream_id,
                "name": name,
                "display_name": resolved_display_name,
                "contents": contents,
                "target_path": target_path,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "created_at": created_at,
                "updated_at": now,
            },
            upsert=True,
        )
        return SandboxFileSummary(
            name=name,
            display_name=resolved_display_name,
            target_path=target_path,
            sha256=sha256,
            size_bytes=size_bytes,
            created_at=created_at,
            updated_at=now,
        )

    async def delete(
        self, org_id: str, upstream_id: str, name: str
    ) -> None:
        await self._sandbox_files.delete_one(
            org_id, {"upstream_id": upstream_id, "name": name},
        )

    async def delete_all(
        self, org_id: str, upstream_id: str
    ) -> None:
        await self._sandbox_files.delete_many(
            org_id, {"upstream_id": upstream_id},
        )
