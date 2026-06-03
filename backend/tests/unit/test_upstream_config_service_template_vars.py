"""``UpstreamConfigService`` integration with the secret store.

Pins:
- ``add_upstream`` / ``update_upstream`` triggers the defensive
  scanner and emits a ``secret_in_json_detected`` log event.
- ``remove_upstream`` cascades to ``template_var_repo.delete_all``
  AND ``sandbox_file_repo.delete_all`` so neither secrets nor
  uploaded files outlive their owning upstream.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from structlog.testing import LogCapture


def make_log_capture() -> LogCapture:
    """Reset structlog config to a fresh in-memory capture per test.

    Inline factory rather than a pytest fixture so the wiring is
    explicit at every call site (per project test conventions).
    """
    capture = LogCapture()
    structlog.configure(processors=[capture])
    return capture

from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService


def _make_service(
    template_var_repo: FileTemplateVarRepository | None = None,
    sandbox_file_repo: FileSandboxFileRepository | None = None,
) -> UpstreamConfigService:
    # ``register_upstream`` is sync — use MagicMock for those
    # collaborators so pytest doesn't emit "coroutine never awaited"
    # warnings. Async methods we actually call (``add``, ``update``,
    # ``unregister_upstream``, the connection-store cleanup) get
    # AsyncMock so awaits are honoured.
    client_manager = MagicMock()
    client_manager.unregister_upstream = AsyncMock()
    tool_registry = MagicMock()
    tool_registry.unregister_upstream = AsyncMock()
    tool_registry.refresh_all = AsyncMock()
    connection_store = AsyncMock()
    connection_store.delete_all_upstream_tokens = AsyncMock(return_value=0)
    return UpstreamConfigService(
        config_store=AsyncMock(),
        client_manager=client_manager,
        tool_registry=tool_registry,
        connection_store=connection_store,
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
    )


def _make_upstream_with_token() -> UpstreamDefinition:
    return UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "ghp_abcdefghijklmnop1234567890ABCDEF"},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


@pytest.mark.asyncio
async def test_add_upstream_logs_scan_finding() -> None:
    capture = make_log_capture()
    service = _make_service(template_var_repo=None)
    upstream = _make_upstream_with_token()
    await service.add_upstream("default", upstream)
    secret_events = [
        e for e in capture.entries
        if e.get("event") == "secret_in_json_detected"
    ]
    assert len(secret_events) == 1
    event = secret_events[0]
    assert event["upstream_id"] == "github"
    assert event["field"] == "env"
    assert event["key"] == "GITHUB_TOKEN"
    assert event["pattern"] == "github_token"
    # Critical: the value must NOT be in the log event.
    assert "ghp_abcdefghijklmnop1234567890ABCDEF" not in str(event)


@pytest.mark.asyncio
async def test_remove_upstream_cascades_to_template_var_repo(
    tmp_path: Path,
) -> None:
    template_var_repo = FileTemplateVarRepository(tmp_path)
    await template_var_repo.set("default", "github", "TOKEN", "value-1234567")
    service = _make_service(template_var_repo=template_var_repo)
    await service.remove_upstream("default", "github")
    assert await template_var_repo.list_summaries("default", "github") == []


@pytest.mark.asyncio
async def test_remove_upstream_cascades_to_sandbox_file_repo(
    tmp_path: Path,
) -> None:
    """An upstream's Sandbox files must not survive the upstream's
    removal — orphan rows in ``mcp_sandbox_files`` were a real bug
    we hit in dev (manual test upstreams left rows behind after
    ``DELETE /api/admin/upstreams/{id}``)."""
    sandbox_file_repo = FileSandboxFileRepository(tmp_path)
    await sandbox_file_repo.set(
        "default", "github", "creds", "secret-bytes",
        "${HOME}/.config/foo",
        display_name="Creds",
    )
    service = _make_service(sandbox_file_repo=sandbox_file_repo)
    await service.remove_upstream("default", "github")
    assert await sandbox_file_repo.list_summaries("default", "github") == []


@pytest.mark.asyncio
async def test_remove_upstream_cascades_to_both_template_vars_and_files(
    tmp_path: Path,
) -> None:
    """Combined cascade: remove_upstream wipes both feeds in one go."""
    template_var_repo = FileTemplateVarRepository(tmp_path / "vars")
    sandbox_file_repo = FileSandboxFileRepository(tmp_path / "files")
    await template_var_repo.set("default", "github", "TOKEN", "value-1234567")
    await sandbox_file_repo.set(
        "default", "github", "creds", "secret-bytes",
        "${HOME}/.config/foo",
    )
    service = _make_service(
        template_var_repo=template_var_repo,
        sandbox_file_repo=sandbox_file_repo,
    )
    await service.remove_upstream("default", "github")
    assert await template_var_repo.list_summaries("default", "github") == []
    assert await sandbox_file_repo.list_summaries("default", "github") == []


@pytest.mark.asyncio
async def test_add_upstream_with_placeholder_does_not_log_finding() -> None:
    capture = make_log_capture()
    service = _make_service(template_var_repo=None)
    upstream = UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    await service.add_upstream("default", upstream)
    secret_events = [
        e for e in capture.entries
        if e.get("event") == "secret_in_json_detected"
    ]
    assert secret_events == []
