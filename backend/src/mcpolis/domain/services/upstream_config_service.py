from __future__ import annotations

from typing import Any

import structlog

from mcpolis.adapters.repositories.connection_store import ConnectionStore
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.sandbox_file_repository import SandboxFileRepository
from mcpolis.domain.ports.template_var_repository import TemplateVarRepository
from mcpolis.domain.services.secret_scanner import scan_for_secrets
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _unwrap_error(e: BaseException) -> str:
    """Extract a readable message from exceptions, unwrapping ExceptionGroups."""
    subs: tuple[BaseException, ...] = getattr(e, "exceptions", ())
    if subs:
        return _unwrap_error(subs[0])
    return str(e)


class UpstreamConfigService:
    """CRUD operations on upstream config with side effects (reconnect, refresh)."""

    def __init__(
        self,
        config_store: UpstreamConfigStore,
        client_manager: UpstreamClientManager,
        tool_registry: ToolRegistry,
        connection_store: ConnectionStore,
        tool_router: ToolRouter | None = None,
        template_var_repo: TemplateVarRepository | None = None,
        sandbox_file_repo: SandboxFileRepository | None = None,
    ) -> None:
        self._store = config_store
        self._client_manager = client_manager
        self._registry = tool_registry
        self._connection_store = connection_store
        self._tool_router = tool_router
        self._template_var_repo = template_var_repo
        self._sandbox_file_repo = sandbox_file_repo

    def set_tool_router(self, tool_router: ToolRouter) -> None:
        """Late-bind the router — resolves the ctor cycle in ``OrgRuntime``."""
        self._tool_router = tool_router

    async def list_upstreams(self, org_id: str) -> list[UpstreamDefinition]:
        return await self._store.get_all(org_id)

    async def get_upstream(
        self, org_id: str, upstream_id: str
    ) -> UpstreamDefinition | None:
        return await self._store.get(org_id, upstream_id)

    def _scan_and_log(
        self, org_id: str, upstream: UpstreamDefinition
    ) -> None:
        """Defensive: log a structured event when env / headers carry
        what looks like a raw credential. Does NOT reject — the user
        may have a legitimate reason. Never logs the value itself.
        """
        env = upstream.stdio.env if upstream.stdio is not None else None
        headers = (
            upstream.http.headers if upstream.http is not None else None
        )
        for finding in scan_for_secrets(env=env, headers=headers):
            logger.warning(
                "secret_in_json_detected",
                org_id=org_id,
                upstream_id=upstream.id,
                field=finding.field,
                key=finding.key,
                pattern=finding.pattern,
                match_preview=finding.match_preview,
            )

    async def add_upstream(
        self, org_id: str, upstream: UpstreamDefinition
    ) -> None:
        """Add an upstream without connecting. User must connect explicitly."""
        self._scan_and_log(org_id, upstream)
        await self._store.add(org_id, upstream)
        self._client_manager.register_upstream(upstream)
        self._registry.register_upstream(upstream)
        if self._tool_router is not None:
            self._tool_router.register_upstream(upstream)

    async def add_upstream_no_refresh(
        self, org_id: str, upstream: UpstreamDefinition
    ) -> None:
        """Add an upstream without connecting or refreshing tools."""
        self._scan_and_log(org_id, upstream)
        await self._store.add(org_id, upstream)
        self._client_manager.register_upstream(upstream)
        self._registry.register_upstream(upstream)
        if self._tool_router is not None:
            self._tool_router.register_upstream(upstream)

    async def update_upstream(
        self, org_id: str, upstream: UpstreamDefinition
    ) -> None:
        self._scan_and_log(org_id, upstream)
        await self._store.update(org_id, upstream)
        self._client_manager.register_upstream(upstream)
        self._registry.register_upstream(upstream)
        if self._tool_router is not None:
            self._tool_router.register_upstream(upstream)
        await self._registry.refresh_all()

    async def update_upstream_with_server_config(
        self,
        org_id: str,
        upstream: UpstreamDefinition,
        server_config: dict[str, Any],
    ) -> None:
        """Update server config in mcp.json and options in upstreams.yaml."""
        self._scan_and_log(org_id, upstream)
        await self._store.update_server_config(org_id, upstream.id, server_config)
        await self._store.update(org_id, upstream)
        self._client_manager.register_upstream(upstream)
        self._registry.register_upstream(upstream)
        if self._tool_router is not None:
            self._tool_router.register_upstream(upstream)
        await self._registry.refresh_all()

    async def remove_upstream(self, org_id: str, upstream_id: str) -> None:
        await self._store.remove(org_id, upstream_id)
        await self._client_manager.unregister_upstream(upstream_id)
        await self._registry.unregister_upstream(upstream_id)
        if self._tool_router is not None:
            self._tool_router.unregister_upstream(upstream_id)
        # Comprehensive purge: not just tokens but the whole key family
        # (client_info, oauth_metadata, enabled marker, failure counters,
        # …). A narrower token-only delete would let a re-add on the same
        # slug resurrect a dead DCR client_info and 400 with
        # ``invalid_client`` forever.
        deleted = await self._connection_store.delete_all_for_upstream(
            org_id, upstream_id
        )
        if deleted:
            logger.info(
                "upstream.removed.state_purged",
                upstream_id=upstream_id,
                org_id=org_id,
                deleted_key_count=deleted,
            )
        # Cascade: env vars don't outlive their owning upstream.
        if self._template_var_repo is not None:
            await self._template_var_repo.delete_all(org_id, upstream_id)
        # Cascade: Sandbox files (contents + target_path metadata)
        # are scoped to the upstream — drop them all so an upstream
        # remove + re-create on the same id doesn't surface stale
        # files at the new upstream.
        if self._sandbox_file_repo is not None:
            await self._sandbox_file_repo.delete_all(org_id, upstream_id)

    async def set_default_arguments(
        self, org_id: str, upstream_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        upstream = await self._store.get(org_id, upstream_id)
        if upstream is None:
            raise ValueError(f"Upstream '{upstream_id}' not found")
        upstream.default_arguments[tool_name] = arguments
        await self._store.update(org_id, upstream)

    async def remove_default_arguments(
        self, org_id: str, upstream_id: str, tool_name: str
    ) -> None:
        upstream = await self._store.get(org_id, upstream_id)
        if upstream is None:
            raise ValueError(f"Upstream '{upstream_id}' not found")
        upstream.default_arguments.pop(tool_name, None)
        await self._store.update(org_id, upstream)

    def connection_status(self) -> dict[str, bool]:
        return {
            uid: self._client_manager.is_connected(uid)
            for uid in self._client_manager.all_upstream_ids
        }
