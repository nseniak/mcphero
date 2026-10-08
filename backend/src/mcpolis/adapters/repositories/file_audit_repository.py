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

from mcpolis.adapters.repositories.atomic_file import write_text_atomic
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
        self._handler = handler

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
                    # Hidden temp name: ``audit.jsonl.tmp`` would match
                    # the rotated-file glob in ``_all_log_files``.
                    write_text_atomic(
                        log_file,
                        "\n".join(kept) + ("\n" if kept else ""),
                        tmp_path=log_file.with_name(f".{log_file.name}.tmp"),
                    )
                    if log_file == self._log_path:
                        self._reopen_live_file()
        return removed

    def _reopen_live_file(self) -> None:
        """Point the log handler at the file now named ``audit.jsonl``.

        The handler keeps the live file open; once a rewrite renamed a new
        file over it, the handler would go on appending to the old,
        unlinked one, and every later row would be lost. Closing the
        stream makes the handler open the path again on its next row
        (append mode reopens a closed stream)."""
        self._handler.acquire()
        try:
            if self._handler.stream is not None:  # pyright: ignore[reportUnnecessaryComparison]
                self._handler.stream.close()
                self._handler.stream = None  # type: ignore[assignment]
        finally:
            self._handler.release()

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
        # The file backend writes a single global jsonl that already
        # carries ``org_id`` per entry. ``search`` ignores org scoping
        # because the standalone deployment effectively has one org.
        return self._scan(
            org_id=None, user_id=user_id, mcp_id=mcp_id, tool=tool,
            action=action, limit=limit, offset=offset, since_iso=since_iso,
        )

    async def search_cross_org(
        self,
        org_id: str | None = None,
        user_id: str | None = None,
        mcp_id: str | None = None,
        tool: str | None = None,
        action: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # Callers are responsible for the superadmin gate.
        return self._scan(
            org_id=org_id, user_id=user_id, mcp_id=mcp_id, tool=tool,
            action=action, limit=limit, offset=offset, since_iso=None,
        )

    def _scan(
        self,
        *,
        org_id: str | None,
        user_id: str | None,
        mcp_id: str | None,
        tool: str | None,
        action: list[str] | None,
        limit: int,
        offset: int,
        since_iso: str | None,
    ) -> list[dict[str, Any]]:
        """Newest-first pass over every log file. Every filter applies
        BEFORE ``offset`` / ``limit`` count a row, so a page is never
        cut short by a filter applied afterwards."""
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
                if org_id and entry.get("org_id") != org_id:
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
