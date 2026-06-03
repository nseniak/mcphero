"""Wire-level test that the HTTP adapter consumes substituted headers.

The pure-function substitution helper and the manager-level
``_resolve_upstream_secrets`` are already covered. The remaining gap
this file closes: when ``UpstreamClientManager._create_task`` is
asked for an HTTP transport, the resulting :class:`HttpConnectionTask`
sees the *substituted* headers (not the raw ``${NAME}`` placeholders).

Done by spying on the constructor: we don't actually want to start
an httpx client during a unit test, just confirm that the upstream
the task receives carries plaintext header values.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)


def _make_http_upstream() -> UpstreamDefinition:
    return UpstreamDefinition(
        id="weather",
        display_name="Weather",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(
            url="https://example.test/mcp",
            headers={
                "Authorization": "Bearer ${API_KEY}",
                "X-Static-Header": "no-secrets-here",
            },
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


@pytest.mark.asyncio
async def test_http_connection_task_receives_substituted_headers(
    tmp_path: Path,
) -> None:
    """End-to-end through ``_create_task``: the spy captures the
    upstream the task is built with, and we assert its headers no
    longer carry ``${API_KEY}``."""
    secret_repo = FileTemplateVarRepository(tmp_path)
    await secret_repo.set("default", "weather", "API_KEY", "live-key-substituted")

    upstream = _make_http_upstream()
    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )

    captured_upstream: list[UpstreamDefinition] = []

    def fake_http_task(upstream_arg: UpstreamDefinition, **_kwargs: object) -> MagicMock:
        captured_upstream.append(upstream_arg)
        task = MagicMock()
        # ``_create_task`` awaits ``task.start()`` — return an awaitable
        # that resolves to a sentinel session.
        async def _start() -> object:
            return object()
        task.start = _start
        return task

    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.HttpConnectionTask",
        side_effect=fake_http_task,
    ):
        await manager._create_task(  # type: ignore[reportPrivateUsage]
            upstream, user_id="default",
        )

    assert len(captured_upstream) == 1
    resolved = captured_upstream[0]
    assert resolved.http is not None
    assert resolved.http.headers == {
        "Authorization": "Bearer live-key-substituted",
        "X-Static-Header": "no-secrets-here",
    }
    # Original is untouched — substitution returns a new object.
    assert upstream.http is not None
    assert upstream.http.headers["Authorization"] == "Bearer ${API_KEY}"


@pytest.mark.asyncio
async def test_http_create_task_propagates_missing_secret_error(
    tmp_path: Path,
) -> None:
    """Unresolved ``${NAME}`` in headers raises before the connection
    task is constructed — proves the fail-closed contract holds for
    HTTP just as it does for stdio."""
    from mcpolis.domain.model.template_var import MissingTemplateVarError

    secret_repo = FileTemplateVarRepository(tmp_path)
    upstream = _make_http_upstream()  # references API_KEY but repo is empty

    manager = UpstreamClientManager(
        upstreams=[upstream], template_var_repo=secret_repo,
    )

    constructor_called = False

    def fake_http_task(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal constructor_called
        constructor_called = True
        return MagicMock()

    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.HttpConnectionTask",
        side_effect=fake_http_task,
    ):
        with pytest.raises(MissingTemplateVarError):
            await manager._create_task(  # type: ignore[reportPrivateUsage]
                upstream, user_id="default",
            )
    assert constructor_called is False, (
        "MissingTemplateVarError must abort BEFORE the connection task is built"
    )
