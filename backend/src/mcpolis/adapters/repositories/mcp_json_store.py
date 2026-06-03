"""Read/write MCP server definitions in standard mcpServers JSON format.

The file format matches Claude Code / ChatGPT MCP configuration:
{
  "mcpServers": {
    "github": { "url": "https://mcp.github.com/sse" },
    "filesystem": { "command": "npx", "args": ["-y", "..."] }
  }
}
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class McpJsonStore:
    """Reads and writes mcp.json in mcpServers format."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, dict[str, Any]]:
        """Read mcpServers dict from the JSON file."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            return data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "mcp_json.read.failed",
                path=str(self._path),
            )
            return {}

    def _write(self, servers: dict[str, dict[str, Any]]) -> None:
        """Write mcpServers dict to the JSON file atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mcpServers": servers}, indent=2))
        tmp.replace(self._path)

    def read_all_sync(self, org_id: str) -> dict[str, dict[str, Any]]:
        """Synchronous read for startup."""
        return self._read()

    async def read_all(self, org_id: str) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return self._read()

    async def add(self, org_id: str, mcp_id: str, server_config: dict[str, Any]) -> None:
        """Add an MCP server. Raises ValueError if id already exists."""
        async with self._lock:
            servers = self._read()
            if mcp_id in servers:
                raise ValueError(f"MCP '{mcp_id}' already exists")
            servers[mcp_id] = server_config
            self._write(servers)

    async def update(self, org_id: str, mcp_id: str, server_config: dict[str, Any]) -> None:
        """Update an MCP server's config. Raises ValueError if not found."""
        async with self._lock:
            servers = self._read()
            if mcp_id not in servers:
                raise ValueError(f"MCP '{mcp_id}' not found")
            servers[mcp_id] = server_config
            self._write(servers)

    async def remove(self, org_id: str, mcp_id: str) -> None:
        """Remove an MCP server. Raises ValueError if not found."""
        async with self._lock:
            servers = self._read()
            if mcp_id not in servers:
                raise ValueError(f"MCP '{mcp_id}' not found")
            del servers[mcp_id]
            self._write(servers)
