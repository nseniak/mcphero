"""File-based upstream config store using mcp.json + config.json.

mcp.json holds standard mcpServers connection definitions (add/remove).
config.json holds MCPolis-specific options (display name, auth mode, etc.).
Reading merges both via load_merged_config().
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.repositories.upstream_config_loader import (
    load_merged_config,
    server_config_from_upstream,
)
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.domain.model.settings import OAuthAppsConfig, UpstreamOptions
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID


class FileUpstreamConfigStore(UpstreamConfigStore):
    """File-based upstream config store backed by mcp.json + config.json."""

    def __init__(
        self,
        mcp_store: McpJsonStore,
        config_store: FileConfigStore,
        oauth_apps: OAuthAppsConfig | None = None,
    ) -> None:
        self._mcp_store = mcp_store
        self._config_store = config_store
        self._oauth_apps = oauth_apps
        self._lock = asyncio.Lock()

    def _read_all(self) -> list[UpstreamDefinition]:
        options = self._config_store.read_upstream_options_sync(DEFAULT_ORG_ID)
        options_dict = {k: v.model_dump() for k, v in options.items()}
        return load_merged_config(
            self._mcp_store.path, options_dict, oauth_apps=self._oauth_apps,
        )

    async def get_all(self, org_id: str) -> list[UpstreamDefinition]:
        async with self._lock:
            return self._read_all()

    async def get(self, org_id: str, upstream_id: str) -> UpstreamDefinition | None:
        async with self._lock:
            for u in self._read_all():
                if u.id == upstream_id:
                    return u
            return None

    async def add(self, org_id: str, upstream: UpstreamDefinition) -> None:
        """Add an MCP — writes connection to mcp.json, options to config.json."""
        await self._mcp_store.add(
            org_id, upstream.id, server_config_from_upstream(upstream),
        )

        # Write options to config store
        options = UpstreamOptions(
            display_name=upstream.display_name,
            auth_mode=upstream.auth.mode.value,
            client_id=upstream.auth.client_id,
            client_secret=upstream.auth.client_secret,
            scopes=upstream.auth.scopes,
            default_arguments=upstream.default_arguments,
        )
        await self._config_store.set_upstream_options(org_id, upstream.id, options)

    async def update(self, org_id: str, upstream: UpstreamDefinition) -> None:
        """Update both options (config.json) and the mcpServers entry
        (mcp.json) so resource-only PUTs (sandbox resources) survive a
        restart."""
        # Verify the MCP exists
        servers = self._mcp_store.read_all_sync(org_id)
        if upstream.id not in servers:
            raise ValueError(f"Upstream '{upstream.id}' not found")

        await self._mcp_store.update(
            org_id, upstream.id, server_config_from_upstream(upstream),
        )
        options = UpstreamOptions(
            display_name=upstream.display_name,
            auth_mode=upstream.auth.mode.value,
            client_id=upstream.auth.client_id,
            client_secret=upstream.auth.client_secret,
            scopes=upstream.auth.scopes,
            default_arguments=upstream.default_arguments,
        )
        await self._config_store.set_upstream_options(org_id, upstream.id, options)

    async def update_server_config(
        self, org_id: str, upstream_id: str, server_config: dict[str, Any]
    ) -> None:
        """Update the connection config in mcp.json."""
        await self._mcp_store.update(org_id, upstream_id, server_config)

    async def remove(self, org_id: str, upstream_id: str) -> None:
        """Remove from both mcp.json and config.json."""
        await self._mcp_store.remove(org_id, upstream_id)
        await self._config_store.remove_upstream_options(org_id, upstream_id)
