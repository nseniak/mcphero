from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog

from mcpolis.domain.ports.tool_catalog_repository import (
    ToolCatalogRepository,
    ToolCatalogSnapshot,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class FileToolCatalogStore(ToolCatalogRepository):
    """JSON file-based tool catalog persistence.

    One file per org under ``<data_dir>/<org_id>/tool_catalog.json``,
    matching the layout used by ``FileOAuthStateRepository``. The
    standalone-mode default-org case becomes
    ``<data_dir>/default/tool_catalog.json``.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = asyncio.Lock()

    def _path(self, org_id: str) -> Path:
        return self._data_dir / org_id / "tool_catalog.json"

    def _read(self, org_id: str) -> dict[str, Any]:
        path = self._path(org_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "tool_catalog_store.read.failed",
                path=str(path),
            )
            return {}

    def _write(self, org_id: str, data: dict[str, Any]) -> None:
        path = self._path(org_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    async def load_all(
        self, org_id: str,
    ) -> dict[str, ToolCatalogSnapshot]:
        async with self._lock:
            data = self._read(org_id)
        out: dict[str, ToolCatalogSnapshot] = {}
        for upstream_id, payload in data.items():
            try:
                out[upstream_id] = ToolCatalogSnapshot.model_validate(payload)
            except Exception:
                logger.warning(
                    "tool_catalog_store.deserialize.failed",
                    org_id=org_id,
                    upstream_id=upstream_id,
                )
        return out

    async def upsert_upstream(
        self,
        org_id: str,
        upstream_id: str,
        snapshot: ToolCatalogSnapshot,
    ) -> None:
        async with self._lock:
            data = self._read(org_id)
            data[upstream_id] = snapshot.model_dump(mode="json")
            self._write(org_id, data)

    async def delete_upstream(
        self, org_id: str, upstream_id: str,
    ) -> None:
        async with self._lock:
            data = self._read(org_id)
            if upstream_id in data:
                data.pop(upstream_id)
                self._write(org_id, data)
