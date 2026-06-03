"""End-to-end test for log redaction wired through the resolver.

Pins the contract that
``UpstreamClientManager._resolve_upstream_template_vars`` configures the
per-upstream ``LogBufferRegion`` redaction set on every session
start, so a subsequent stderr write that includes a substituted
secret value lands in the operator's log buffer as
``[REDACTED:NAME]`` instead of plaintext.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
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


@pytest.mark.asyncio
async def test_secret_value_is_redacted_in_log_buffer(
    tmp_path: Path,
) -> None:
    """``is_secret=true`` value substituted into ``stdio.env`` is
    redacted from a subsequent ``log_buffer.write`` call."""
    repo = FileTemplateVarRepository(tmp_path)
    await repo.set(
        "default", "github", "GITHUB_TOKEN",
        "ghp_supersecretvalue123",
        is_secret=True,
    )
    upstream = _make_stdio_upstream(env={"TOKEN": "${GITHUB_TOKEN}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.env == {"TOKEN": "ghp_supersecretvalue123"}

    # Simulate a sandbox stderr write that echoes the substituted
    # value (e.g. an upstream tool printing its env at startup).
    buf = manager.log_buffers.get_or_create(upstream.id)
    buf.write("auth header was Bearer ghp_supersecretvalue123 OK")

    output = buf.get_output()
    assert "ghp_supersecretvalue123" not in output
    assert "[REDACTED:GITHUB_TOKEN]" in output


@pytest.mark.asyncio
async def test_plain_value_is_not_redacted_in_log_buffer(
    tmp_path: Path,
) -> None:
    """``is_secret=false`` values are operator-visible by design and
    must NOT be added to the redaction set."""
    repo = FileTemplateVarRepository(tmp_path)
    await repo.set(
        "default", "github", "FEATURE_FLAG",
        "enable_widget_v2_long_enough",
        is_secret=False,
    )
    upstream = _make_stdio_upstream(env={"FLAG": "${FEATURE_FLAG}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    assert resolved.stdio.env == {"FLAG": "enable_widget_v2_long_enough"}

    buf = manager.log_buffers.get_or_create(upstream.id)
    buf.write("running with FLAG=enable_widget_v2_long_enough")

    output = buf.get_output()
    assert "enable_widget_v2_long_enough" in output
    assert "[REDACTED:FEATURE_FLAG]" not in output


@pytest.mark.asyncio
async def test_redaction_set_refreshes_on_subsequent_resolve(
    tmp_path: Path,
) -> None:
    """Rotation contract: the redaction set follows the latest
    resolved value, not the value seen at first session start."""
    repo = FileTemplateVarRepository(tmp_path)
    await repo.set(
        "default", "github", "GITHUB_TOKEN",
        "first_value_long_enough",
        is_secret=True,
    )
    upstream = _make_stdio_upstream(env={"TOKEN": "${GITHUB_TOKEN}"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=repo,
    )
    await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]

    # Rotate the secret. The next resolve must reset the redaction
    # set so the OLD value passes through (it's no longer secret-
    # by-association — only the live value masks).
    await repo.set(
        "default", "github", "GITHUB_TOKEN",
        "second_value_long_enough",
        is_secret=True,
    )
    await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]

    buf = manager.log_buffers.get_or_create(upstream.id)
    buf.write("saw first_value_long_enough and second_value_long_enough")
    output = buf.get_output()
    assert "first_value_long_enough" in output
    assert "second_value_long_enough" not in output
    assert "[REDACTED:GITHUB_TOKEN]" in output


@pytest.mark.asyncio
async def test_no_redaction_when_no_references(tmp_path: Path) -> None:
    """An upstream that doesn't reference any variable doesn't
    populate the redaction set; the buffer captures plaintext."""
    repo = FileTemplateVarRepository(tmp_path)
    await repo.set(
        "default", "github", "UNUSED_TOKEN",
        "would_be_redacted_if_referenced",
        is_secret=True,
    )
    upstream = _make_stdio_upstream(env={"FOO": "literal-value"})
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=repo,
    )
    await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]

    buf = manager.log_buffers.get_or_create(upstream.id)
    buf.write("captured would_be_redacted_if_referenced literally")
    assert "would_be_redacted_if_referenced" in buf.get_output()
