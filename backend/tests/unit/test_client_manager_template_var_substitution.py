"""End-to-end test for ``${NAME}`` substitution at task-creation time.

Targets ``UpstreamClientManager._resolve_upstream_template_vars`` — the
single layer responsible for replacing references in stdio ``env``
and HTTP ``headers`` before any connection task sees them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.template_var import MissingTemplateVarError
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
)


def _make_stdio_upstream(
    env: dict[str, str] | None = None,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env=env or {},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def _make_http_upstream(
    headers: dict[str, str] | None = None,
) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="weather",
        display_name="Weather",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(
            url="https://example.test/mcp",
            headers=headers or {},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


@pytest.mark.asyncio
async def test_resolve_substitutes_stdio_env(tmp_path: Path) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "github", "GH_TOKEN", "ghp_resolved_value")
    upstream = _make_stdio_upstream(env={"GITHUB_TOKEN": "${GH_TOKEN}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.env == {"GITHUB_TOKEN": "ghp_resolved_value"}
    # Original is untouched.
    assert upstream.stdio is not None
    assert upstream.stdio.env == {"GITHUB_TOKEN": "${GH_TOKEN}"}


@pytest.mark.asyncio
async def test_resolve_substitutes_home_with_provider_home(
    tmp_path: Path,
) -> None:
    """``${HOME}`` resolves to the provider's home passed by the caller,
    not a hardcoded constant. The manager passes the active provider's
    ``sandbox_home`` (a local-subprocess per-session temp dir here) so
    the substituted value matches the spawned process's real ``$HOME``.
    """
    secret_repo = FileTemplateVarRepository(tmp_path)
    upstream = _make_stdio_upstream(
        env={"CRED_PATH": "${HOME}/.config/cred.json"},
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    home = "/tmp/mcpolis-local-home-sess123"
    resolved = await manager._resolve_upstream_template_vars(  # type: ignore[reportPrivateUsage]
        upstream, sandbox_home=home,
    )
    assert resolved.stdio is not None
    assert resolved.stdio.env == {"CRED_PATH": f"{home}/.config/cred.json"}


@pytest.mark.asyncio
async def test_resolve_sandbox_file_target_path_uses_provider_home(
    tmp_path: Path,
) -> None:
    """A ``${HOME}``-templated sandbox-file ``target_path`` materializes
    under the provider's home, so the file lands where the spawned
    process (whose ``$HOME`` is that same dir) looks for it."""
    file_repo = FileSandboxFileRepository(tmp_path / "files")
    await file_repo.set(
        "default", "github", "cred",
        contents="secret", target_path="${HOME}/.config/cred.json",
    )
    upstream = _make_stdio_upstream()
    manager = UpstreamClientManager(
        upstreams=[upstream], sandbox_file_repo=file_repo,
    )
    home = "/tmp/mcpolis-local-home-sess456"
    materialized = await manager._resolve_sandbox_files(  # type: ignore[reportPrivateUsage]
        upstream, sandbox_home=home,
    )
    assert len(materialized) == 1
    assert materialized[0].target_path == f"{home}/.config/cred.json"
    assert materialized[0].contents == "secret"


@pytest.mark.asyncio
async def test_resolve_substitutes_http_headers(tmp_path: Path) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "weather", "API_KEY", "live-key-123")
    upstream = _make_http_upstream(
        headers={"Authorization": "Bearer ${API_KEY}"},
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.http is not None
    assert resolved.http.headers == {"Authorization": "Bearer live-key-123"}


@pytest.mark.asyncio
async def test_resolve_unresolved_raises_missing_secret_error(
    tmp_path: Path,
) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    upstream = _make_stdio_upstream(env={"GITHUB_TOKEN": "${MISSING}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    with pytest.raises(MissingTemplateVarError) as exc:
        await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert exc.value.name == "MISSING"
    assert exc.value.upstream_id == "github"


@pytest.mark.asyncio
async def test_resolve_passes_through_static_values(
    tmp_path: Path,
) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    upstream = _make_stdio_upstream(env={"NODE_ENV": "production"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.env == {"NODE_ENV": "production"}


@pytest.mark.asyncio
async def test_resolve_with_no_secret_repo_passes_through(tmp_path: Path) -> None:
    upstream = _make_stdio_upstream(env={"NODE_ENV": "production"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=None,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved is upstream


@pytest.mark.asyncio
async def test_resolve_with_no_secret_repo_fails_closed_on_placeholder(
    tmp_path: Path,
) -> None:
    """If a placeholder exists but no repo is wired, the user gets
    the literal ``${NAME}`` value — broken but visible. Compare with
    repo-but-unresolved which raises. This is the legacy / test-factory
    fallback path."""
    upstream = _make_stdio_upstream(env={"GITHUB_TOKEN": "${MISSING}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=None,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    # Pass-through; the user sees ``${MISSING}`` in their env.
    assert resolved is upstream


# --- Substitution covers every user-controlled string field ---


@pytest.mark.asyncio
async def test_resolve_substitutes_stdio_args(tmp_path: Path) -> None:
    """``args`` carry value-bearing flags too — a regression for the
    ``python3 -c "print(${VAR})"`` case where the literal token
    reached the sandbox and produced a Python SyntaxError."""
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "github", "VAR", "42")
    upstream = UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="python3",
            args=["-c", "print(${VAR});"],
            env={},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.args == ["-c", "print(42);"]


@pytest.mark.asyncio
async def test_resolve_substitutes_stdio_command(tmp_path: Path) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "github", "PY", "/usr/bin/python3")
    upstream = UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="${PY}", args=[], env={},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.command == "/usr/bin/python3"


@pytest.mark.asyncio
async def test_resolve_substitutes_http_url(tmp_path: Path) -> None:
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "weather", "ORG", "acme")
    upstream = UpstreamDefinition(
        id="weather",
        display_name="Weather",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(
            url="https://example.test/${ORG}/mcp",
            headers={},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.http is not None
    assert resolved.http.url == "https://example.test/acme/mcp"


@pytest.mark.asyncio
async def test_resolve_honours_backslash_escape_in_args(
    tmp_path: Path,
) -> None:
    """``\\${NAME}`` round-trips literally as ``${NAME}`` so a
    downstream tool that itself reads host env vars (e.g. an inline
    Python ``-c`` snippet referencing ``os.environ``) can pass an
    unsubstituted token through."""
    secret_repo = FileTemplateVarRepository(tmp_path)
    upstream = UpstreamDefinition(
        id="github",
        display_name="GitHub",
        transport=TransportType.stdio,
        stdio=StdioTransportConfig(
            command="python3",
            args=["-c", r"import os; print(os.environ['\${HOST_VAR}'])"],
            env={},
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.args == [
        "-c",
        "import os; print(os.environ['${HOST_VAR}'])",
    ]
