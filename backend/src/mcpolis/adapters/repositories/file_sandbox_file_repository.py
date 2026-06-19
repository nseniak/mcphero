"""File-backed sandbox-file store for standalone mode.

Persists to ``<data_dir>/sandbox_files.json`` with the layout::

    {
      "<org_id>": {
        "<upstream_id>": {
          "<name>": {
            "display_name": "GCP service account",
            "contents": "<plaintext>",
            "target_path": "${HOME}/.config/...",
            "sha256": "<hex>",
            "size_bytes": 1234,
            "created_at": "<iso8601>",
            "updated_at": "<iso8601>"
          }
        }
      }
    }

``<name>`` is the URL-safe storage key; ``display_name`` is the
free-form human label. Legacy rows that pre-date ``display_name``
round-trip as ``display_name == name`` so an upgrade doesn't lose
the listing label.

Plaintext on disk by design — matches the existing template-vars +
``mcp.json`` precedent in standalone mode (the user runs MCP Hero on
their own machine; encryption with a key on the same disk would be
security theatre). Cloud mode uses
:class:`mcpolis.adapters.repositories.mongo_sandbox_file_repository.MongoSandboxFileRepository`,
which encrypts the ``contents`` field at rest.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from mcpolis.domain.model.sandbox_file import (
    SandboxFile,
    SandboxFileSummary,
    compute_sha256,
)
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class FileSandboxFileRepository(SandboxFileRepository):
    def __init__(self, data_dir: Path) -> None:
        self._path: Path = data_dir / "sandbox_files.json"
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "sandbox_files.file.read_failed", path=str(self._path),
            )
            return {}

    def _write(
        self,
        data: dict[str, dict[str, dict[str, dict[str, Any]]]],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)

    @staticmethod
    def _display_name(name: str, record: dict[str, Any]) -> str:
        # Legacy rows (no ``display_name``) fall back to the storage
        # key so the listing always renders something readable.
        raw = record.get("display_name")
        if isinstance(raw, str) and raw.strip():
            return raw
        return name

    @staticmethod
    def _summary_from_record(
        name: str, record: dict[str, Any]
    ) -> SandboxFileSummary:
        return SandboxFileSummary(
            name=name,
            display_name=FileSandboxFileRepository._display_name(name, record),
            target_path=str(record.get("target_path", "")),
            sha256=str(record.get("sha256", "")),
            size_bytes=int(record.get("size_bytes", 0)),
            created_at=datetime.fromisoformat(record["created_at"]),
            updated_at=datetime.fromisoformat(record["updated_at"]),
        )

    @staticmethod
    def _full_from_record(name: str, record: dict[str, Any]) -> SandboxFile:
        contents = record.get("contents")
        if not isinstance(contents, str):
            contents = ""
        return SandboxFile(
            name=name,
            display_name=FileSandboxFileRepository._display_name(name, record),
            contents=contents,
            target_path=str(record.get("target_path", "")),
            sha256=str(record.get("sha256", "")),
            size_bytes=int(record.get("size_bytes", 0)),
            created_at=datetime.fromisoformat(record["created_at"]),
            updated_at=datetime.fromisoformat(record["updated_at"]),
        )

    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFileSummary]:
        async with self._lock:
            data = self._read()
            block = data.get(org_id, {}).get(upstream_id, {})
            return [
                self._summary_from_record(name, record)
                for name, record in sorted(block.items())
            ]

    async def list_full(
        self, org_id: str, upstream_id: str
    ) -> list[SandboxFile]:
        async with self._lock:
            data = self._read()
            block = data.get(org_id, {}).get(upstream_id, {})
            return [
                self._full_from_record(name, record)
                for name, record in sorted(block.items())
            ]

    async def get(
        self, org_id: str, upstream_id: str, name: str
    ) -> SandboxFile | None:
        async with self._lock:
            data = self._read()
            record = data.get(org_id, {}).get(upstream_id, {}).get(name)
            if record is None:
                return None
            return self._full_from_record(name, record)

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
        # supply one. Keeps legacy scripted writes (and the migration
        # path for pre-display-name rows) free of empty labels.
        resolved_display_name = (
            display_name.strip() if display_name and display_name.strip()
            else name
        )
        async with self._lock:
            data = self._read()
            org_block = data.setdefault(org_id, {})
            upstream_block = org_block.setdefault(upstream_id, {})
            existing = upstream_block.get(name)
            created_at = (
                datetime.fromisoformat(existing["created_at"])
                if existing is not None and "created_at" in existing
                else now
            )
            record: dict[str, Any] = {
                "display_name": resolved_display_name,
                "contents": contents,
                "target_path": target_path,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "created_at": created_at.isoformat(),
                "updated_at": now.isoformat(),
            }
            upstream_block[name] = record
            self._write(data)
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
        async with self._lock:
            data = self._read()
            block = data.get(org_id, {}).get(upstream_id, {})
            if name not in block:
                return
            del block[name]
            if not block:
                data.get(org_id, {}).pop(upstream_id, None)
                if not data.get(org_id):
                    data.pop(org_id, None)
            self._write(data)

    async def delete_all(
        self, org_id: str, upstream_id: str
    ) -> None:
        async with self._lock:
            data = self._read()
            org_block = data.get(org_id)
            if org_block is None or upstream_id not in org_block:
                return
            del org_block[upstream_id]
            if not org_block:
                data.pop(org_id, None)
            self._write(data)

    async def delete_all_for_org(self, org_id: str) -> int:
        # Org-deletion cascade. Top-level keyed by org_id, so pop the
        # whole block; count the leaf (upstream, name) files removed.
        async with self._lock:
            data = self._read()
            org_block = data.pop(org_id, None)
            if org_block is None:
                return 0
            removed = sum(len(names) for names in org_block.values())
            self._write(data)
            return removed
