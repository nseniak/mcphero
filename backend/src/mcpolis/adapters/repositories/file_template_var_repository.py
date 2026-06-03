"""File-backed env-var store for standalone mode.

Persists to ``<data_dir>/template_vars.json`` with the layout::

    {
      "<org_id>": {
        "<upstream_id>": {
          "<NAME>": {
            "value": "<plaintext>",
            "is_secret": true,
            "last_four": "wXY4" | null,
            "created_at": "<iso8601>",
            "updated_at": "<iso8601>"
          }
        }
      }
    }

Plaintext on disk by design — matches the existing ``mcp.json`` /
``config.json`` precedent in standalone mode (the user runs MCP Hero on
their own machine; encryption with a key on the same disk would be
security theatre). Cloud mode uses
:class:`mcpolis.adapters.repositories.mongo_template_var_repository.MongoTemplateVarRepository`,
which encrypts the ``value`` field at rest regardless of ``is_secret``.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from mcpolis.domain.model.template_var import (
    TemplateVarSummary,
    compute_last_four,
)
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class FileTemplateVarRepository(TemplateVarRepository):
    def __init__(self, data_dir: Path) -> None:
        # Same directory the FileConnectionStore writes to so a single
        # ``template_vars.json`` lives alongside the rest of standalone state.
        self._path: Path = data_dir / "template_vars.json"
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
                "template_vars.file.read_failed", path=str(self._path),
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
    def _summary_from_record(
        name: str, record: dict[str, Any]
    ) -> TemplateVarSummary:
        # Default ``is_secret=True`` for back-compat with v1 records
        # that pre-date the field.
        is_secret = bool(record.get("is_secret", True))
        # The list response now carries ``value`` for both kinds —
        # the UI obfuscates password rows by default and exposes an
        # eye toggle (1Password-style); reveal stays on the dashboard
        # API surface (admin-only, encrypted at rest in cloud mode).
        stored_value = record.get("value")
        value: str | None = (
            stored_value if isinstance(stored_value, str) else None
        )
        return TemplateVarSummary(
            name=name,
            is_secret=is_secret,
            value=value,
            last_four=record.get("last_four"),
            created_at=datetime.fromisoformat(record["created_at"]),
            updated_at=datetime.fromisoformat(record["updated_at"]),
        )

    async def list_summaries(
        self, org_id: str, upstream_id: str
    ) -> list[TemplateVarSummary]:
        async with self._lock:
            data = self._read()
            org_template_vars = data.get(org_id, {}).get(upstream_id, {})
            return [
                self._summary_from_record(name, record)
                for name, record in sorted(org_template_vars.items())
            ]

    async def get_value(
        self, org_id: str, upstream_id: str, name: str
    ) -> str | None:
        async with self._lock:
            data = self._read()
            record = data.get(org_id, {}).get(upstream_id, {}).get(name)
            if record is None:
                return None
            value = record.get("value")
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
            # Replace contract: existing record's ``is_secret`` wins
            # (default ``True`` for v1 records). Caller can't flip
            # secrecy by re-PUTing — that would defeat the
            # never-show-saved-secret guarantee.
            effective_is_secret: bool = (
                bool(existing.get("is_secret", True))
                if existing is not None
                else is_secret
            )
            record: dict[str, Any] = {
                "value": value,
                "is_secret": effective_is_secret,
                "last_four": compute_last_four(value),
                "created_at": created_at.isoformat(),
                "updated_at": now.isoformat(),
            }
            upstream_block[name] = record
            self._write(data)
            return TemplateVarSummary(
                name=name,
                is_secret=effective_is_secret,
                value=value,
                last_four=record["last_four"],
                created_at=created_at,
                updated_at=now,
            )

    async def delete(
        self, org_id: str, upstream_id: str, name: str
    ) -> None:
        async with self._lock:
            data = self._read()
            upstream_block = data.get(org_id, {}).get(upstream_id, {})
            if name not in upstream_block:
                return
            del upstream_block[name]
            # Prune empty parents so the file doesn't fill with husks.
            if not upstream_block:
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
