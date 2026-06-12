"""File-backed service-token registry for standalone mode.

Persists to ``<data_dir>/service_tokens.json`` with the layout::

    {
      "<org_id>": {
        "<label>": {
          "token_hash": "<sha256 hex>",
          "role_name": "<role>",
          "created_by": "<admin email>",
          "created_at": "<iso8601>",
          "last_used_at": "<iso8601>" | null
        }
      }
    }

Only the sha256 hash of the token is ever written; nothing in this
file is secret (same reasoning as the Mongo implementation — see the
port docstring).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from mcpolis.domain.model.service_token import ServiceTokenRecord
from mcpolis.domain.ports.service_token_repository import (
    DuplicateServiceTokenLabelError,
    ServiceTokenRepository,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _record_to_json(record: ServiceTokenRecord) -> dict[str, Any]:
    return {
        "token_hash": record.token_hash,
        "role_name": record.role_name,
        "created_by": record.created_by,
        "created_at": record.created_at.isoformat(),
        "last_used_at": (
            record.last_used_at.isoformat()
            if record.last_used_at is not None
            else None
        ),
    }


def _record_from_json(
    org_id: str, label: str, raw: dict[str, Any]
) -> ServiceTokenRecord:
    last_used = raw.get("last_used_at")
    return ServiceTokenRecord(
        token_hash=raw["token_hash"],
        org_id=org_id,
        label=label,
        role_name=raw["role_name"],
        created_by=raw["created_by"],
        created_at=datetime.fromisoformat(raw["created_at"]),
        last_used_at=(
            datetime.fromisoformat(last_used) if last_used else None
        ),
    )


class FileServiceTokenRepository(ServiceTokenRepository):
    def __init__(self, data_dir: Path) -> None:
        self._path: Path = data_dir / "service_tokens.json"
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, dict[str, dict[str, Any]]]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "service_tokens.file.read_failed", path=str(self._path),
            )
            return {}

    def _write(self, data: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self._path)

    async def create(self, record: ServiceTokenRecord) -> None:
        async with self._lock:
            data = self._read()
            org_block = data.setdefault(record.org_id, {})
            if record.label in org_block:
                raise DuplicateServiceTokenLabelError(record.label)
            org_block[record.label] = _record_to_json(record)
            self._write(data)

    async def get_by_hash(
        self, token_hash: str
    ) -> ServiceTokenRecord | None:
        async with self._lock:
            data = self._read()
            for org_id, org_block in data.items():
                for label, raw in org_block.items():
                    if raw.get("token_hash") == token_hash:
                        return _record_from_json(org_id, label, raw)
            return None

    async def list_for_org(self, org_id: str) -> list[ServiceTokenRecord]:
        async with self._lock:
            data = self._read()
            org_block = data.get(org_id, {})
            return [
                _record_from_json(org_id, label, raw)
                for label, raw in sorted(org_block.items())
            ]

    async def get_by_label(
        self, org_id: str, label: str
    ) -> ServiceTokenRecord | None:
        async with self._lock:
            data = self._read()
            raw = data.get(org_id, {}).get(label)
            if raw is None:
                return None
            return _record_from_json(org_id, label, raw)

    async def delete_by_label(self, org_id: str, label: str) -> bool:
        async with self._lock:
            data = self._read()
            org_block = data.get(org_id, {})
            if label not in org_block:
                return False
            del org_block[label]
            if not org_block:
                data.pop(org_id, None)
            self._write(data)
            return True

    async def delete_for_org(self, org_id: str) -> int:
        async with self._lock:
            data = self._read()
            org_block = data.pop(org_id, None)
            if not org_block:
                return 0
            self._write(data)
            return len(org_block)

    async def touch_last_used(
        self, token_hash: str, when: datetime
    ) -> None:
        async with self._lock:
            data = self._read()
            for org_block in data.values():
                for raw in org_block.values():
                    if raw.get("token_hash") == token_hash:
                        raw["last_used_at"] = when.isoformat()
                        self._write(data)
                        return
