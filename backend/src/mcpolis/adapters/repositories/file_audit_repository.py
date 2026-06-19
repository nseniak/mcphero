from __future__ import annotations

import asyncio
import glob
import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from pythonjsonlogger.json import JsonFormatter

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.events import Event
from mcpolis.domain.ports.event_stream import EventStream

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class FileAuditRepository(AuditRepository):
    """JSONL file-backed audit repository with daily rotation."""

    def __init__(
        self,
        log_path: Path,
        event_bus: EventStream | None = None,
        retention_days: int = 7,
    ) -> None:
        self._log_path = log_path
        self._lock = asyncio.Lock()
        self._event_bus = event_bus
        self._retention_days = retention_days
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Set up a dedicated logger with TimedRotatingFileHandler
        self._audit_logger = logging.getLogger(f"mcpolis.audit.{id(self)}")
        self._audit_logger.setLevel(logging.INFO)
        self._audit_logger.propagate = False

        handler = TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            backupCount=retention_days,
            utc=True,
        )
        handler.setFormatter(JsonFormatter(timestamp=False))
        handler.namer = _rotated_name
        self._audit_logger.addHandler(handler)

    async def log(self, org_id: str, entry: AuditEntry) -> None:
        data = entry.model_dump()
        async with self._lock:
            self._audit_logger.info("", extra=data)
        logger.debug(
            "audit.entry.logged",
            user=entry.user_id,
            tool=entry.tool,
            policy_decision=entry.policy_decision,
        )
        if self._event_bus is not None:
            self._event_bus.publish(org_id, Event(
                type="audit_entry",
                payload=data,
            ))

    async def delete_for_org(self, org_id: str) -> int:
        """Drop every audit line belonging to ``org_id`` (org-deletion
        cascade). Rewrites each JSONL file keeping only other orgs' rows,
        so a multi-org file (unusual in standalone, but possible in tests)
        stays intact for the survivors. Returns the count removed."""
        removed = 0
        async with self._lock:
            for log_file in self._all_log_files():
                lines = log_file.read_text().splitlines()
                kept: list[str] = []
                for line in lines:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        kept.append(line)
                        continue
                    if entry.get("org_id") == org_id:
                        removed += 1
                    else:
                        kept.append(line)
                if len(kept) != len(lines):
                    log_file.write_text(
                        "\n".join(kept) + ("\n" if kept else "")
                    )
        return removed

    def _all_log_files(self) -> list[Path]:
        """Return all audit log files (current + rotated), newest first."""
        base = str(self._log_path)
        rotated = sorted(glob.glob(f"{base}.*"), reverse=True)
        files: list[Path] = []
        if self._log_path.exists():
            files.append(self._log_path)
        files.extend(Path(p) for p in rotated)
        return files

    async def search(
        self,
        org_id: str,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        skipped = 0
        for log_file in self._all_log_files():
            if len(results) >= limit:
                break
            lines = log_file.read_text().strip().splitlines()
            for line in reversed(lines):
                if len(results) >= limit:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if user_id and entry.get("user_id") != user_id:
                    continue
                if mcp_id and entry.get("upstream_id") != mcp_id:
                    continue
                if tool and tool.lower() not in entry.get("tool", "").lower():
                    continue
                if action and entry.get("action", "tool_call") not in action:
                    continue
                if since_iso:
                    ts = entry.get("timestamp")
                    # Rows without a timestamp pass through — we'd
                    # rather show unknown-age data than blackhole it.
                    # Modern rows always carry one.
                    if isinstance(ts, str) and ts < since_iso:
                        continue
                if skipped < offset:
                    skipped += 1
                    continue
                results.append(entry)
        return results

    async def search_cross_org(
        self,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # The file backend writes a single global jsonl that already
        # carries ``org_id`` per entry — ``search`` ignores org scoping
        # because the standalone deployment effectively has one org. The
        # cross-org search is the same pass with no org filter applied;
        # callers are responsible for the superadmin gate.
        results: list[dict[str, Any]] = []
        skipped = 0
        for log_file in self._all_log_files():
            if len(results) >= limit:
                break
            lines = log_file.read_text().strip().splitlines()
            for line in reversed(lines):
                if len(results) >= limit:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if user_id and entry.get("user_id") != user_id:
                    continue
                if mcp_id and entry.get("upstream_id") != mcp_id:
                    continue
                if tool and tool.lower() not in entry.get("tool", "").lower():
                    continue
                if action and entry.get("action", "tool_call") not in action:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                results.append(entry)
        return results

    async def get_filter_values(self, org_id: str) -> dict[str, list[str]]:
        user_ids: set[str] = set()
        upstream_ids: set[str] = set()
        for log_file in self._all_log_files():
            for line in log_file.read_text().strip().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if uid := entry.get("user_id"):
                    user_ids.add(uid)
                if mid := entry.get("upstream_id"):
                    upstream_ids.add(mid)
        return {
            "user_ids": sorted(user_ids),
            "upstream_ids": sorted(upstream_ids),
        }


def _rotated_name(default_name: str) -> str:
    """Name rotated files as audit.jsonl.2026-04-01 instead of audit.jsonl.20260401."""
    return default_name
