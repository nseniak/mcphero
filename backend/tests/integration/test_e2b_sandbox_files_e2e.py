"""Real-SDK end-to-end proof that the per-MCP Sandbox-files hook
materialises uploaded contents inside an E2B sandbox before the MCP
process starts.

Two scenarios:

1. **Happy path** — one Sandbox file with ``${HOME}``-prefixed
   ``target_path`` lands at ``/home/user/...``; a tiny stdio MCP
   reads it back via ``open()`` and prints the contents.
2. **GCP-style recipe** — Sandbox file ``GCP_CRED`` with
   ``target_path = ${HOME}/.config/gcloud/credentials.json``, paired
   with a Variable ``GOOGLE_APPLICATION_CREDENTIALS`` whose value is
   the same ``${HOME}/...`` literal. After resolution both sides
   point at the same absolute path; the file body at that path
   matches what was uploaded.

Both run a self-contained Python stdio MCP via ``python3 -c
"<inline>"`` so the test doesn't depend on a public URL hosting
the project's ``stdio_test_mcp_server.py`` fixture. The inline
script ships an MCP server with one ``read_file(path)`` tool +
one ``read_env(name)`` tool — same surface as the project fixture,
just inlined to keep the integration test infra-free.

Skips when ``E2B_API_KEY`` is unset.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from mcp.client.session import ClientSession

from mcpolis.adapters.repositories.file_sandbox_file_repository import (
    FileSandboxFileRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.sandbox_e2b import (
    E2BSandboxService,
    RealE2BClient,
)
from mcpolis.adapters.sandbox_e2b.client import E2BSDKError
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.services.sandbox_service import SandboxResources
from tests.unit.factories import make_upstream_definition

E2B_API_KEY: str | None = os.environ.get("E2B_API_KEY") or None
TEST_RUN_ID: str = uuid.uuid4().hex[:12]

pytestmark = pytest.mark.skipif(
    E2B_API_KEY is None,
    reason="E2B_API_KEY not set — real-SDK Sandbox-files tests skipped",
)


# Inline stdio MCP shipped via ``python3 -c``. Two tools:
#  - ``read_file(path)`` returns the contents.
#  - ``read_env(name)`` returns the env var value.
# Implemented against ``mcp.server.fastmcp`` which is pre-installed
# in the python E2B template via the ``mcp-server-time`` /
# ``mcp-server-fetch`` pre-warm chain that pulls the ``mcp`` SDK as
# a transitive dep.
_INLINE_MCP_SCRIPT = (
    "import os\n"
    "from mcp.server.fastmcp import FastMCP\n"
    "s = FastMCP('inline-mcp')\n"
    "@s.tool()\n"
    "def read_file(path: str) -> str:\n"
    "    return open(path).read()\n"
    "@s.tool()\n"
    "def read_env(name: str) -> str:\n"
    "    return os.environ.get(name, '')\n"
    "s.run(transport='stdio')\n"
)


def _make_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


def _is_template_missing_error(exc: BaseException) -> bool:
    if not isinstance(exc, E2BSDKError):
        return False
    needle = (exc.detail + " " + exc.error_class).lower()
    return "template" in needle and ("not found" in needle or "404" in needle)


def _is_inline_mcp_missing(exc: BaseException) -> bool:
    """Detect the case where the inline MCP fails because ``mcp``
    isn't pre-installed in the active python template (rare; the
    standard mcpolis-python templates pre-warm uvx to pull it in)."""
    msg = str(exc).lower()
    return "modulenotfounderror" in msg or "no module named 'mcp'" in msg


@pytest.mark.asyncio
async def test_sandbox_file_materializes_at_resolved_target_path(
    tmp_path: Path,
) -> None:
    """One Sandbox file written via ``materialize_files`` lands at
    ``${HOME}/...`` and the MCP can ``open()`` it back."""
    assert E2B_API_KEY is not None
    org_id = f"acme-{TEST_RUN_ID}-mat"
    upstream = make_upstream_definition(
        id=f"e2e-mat-{TEST_RUN_ID}", command="uvx",
    )
    # ``uvx`` is in the command allowlist; we ask it to run a short
    # one-liner that imports ``mcp.server.fastmcp`` and starts the
    # inline server. ``--from`` makes uvx install the ``mcp`` package
    # before exec'ing python.
    upstream.stdio.args = [  # type: ignore[union-attr]
        "--from", "mcp",
        "python", "-c", _INLINE_MCP_SCRIPT,
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    file_repo = FileSandboxFileRepository(tmp_path)
    var_repo = FileTemplateVarRepository(tmp_path)
    expected_body = f"hello-from-sandbox-files-{TEST_RUN_ID}"
    await file_repo.set(
        org_id, upstream.id, "TEST_FILE",
        expected_body,
        "${HOME}/test-sandbox-file.txt",
    )

    manager = UpstreamClientManager(
        upstreams=[upstream], org_id=org_id,
        template_var_repo=var_repo, sandbox_file_repo=file_repo,
    )
    materialize = await manager._resolve_sandbox_files(upstream)  # type: ignore[reportPrivateUsage]
    assert len(materialize) == 1
    assert materialize[0].target_path == "/home/user/test-sandbox-file.txt"
    assert materialize[0].contents == expected_body

    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-mat-{TEST_RUN_ID}",
        on_timeout_seconds=60,
    )
    try:
        async with service.session(
            session_id=f"e2e-mat-{TEST_RUN_ID}",
            org_id=org_id,
            upstream=upstream,
            resources=_make_resources(),
            denylist=(),
            materialize_files=materialize,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream,
                sandbox_session.write_stream,
            )
            async with session:
                await asyncio.wait_for(session.initialize(), timeout=120.0)
                result = await asyncio.wait_for(
                    session.call_tool(
                        "read_file",
                        {"path": "/home/user/test-sandbox-file.txt"},
                    ),
                    timeout=30.0,
                )
                assert result.content
                # First text content carries the file body.
                texts = [
                    c.text for c in result.content
                    if getattr(c, "type", None) == "text"
                ]
                assert any(expected_body in t for t in texts), (
                    f"file body not in tool result: {texts!r}"
                )
    except E2BSDKError as exc:
        if _is_template_missing_error(exc):
            pytest.skip("mcpolis E2B templates not published on this account")
        raise
    except Exception as exc:
        if _is_inline_mcp_missing(exc):
            pytest.skip("mcp package not available in active python template")
        raise


@pytest.mark.asyncio
async def test_sandbox_file_target_path_with_spaces_materializes(
    tmp_path: Path,
) -> None:
    """A target_path containing spaces (e.g. a macOS screenshot name
    reached via ``${HOME}/${FILE_NAME}``) must materialise and the
    upstream must boot.

    Regression for the unquoted-chmod bug: ``write_file`` ran
    ``chmod 600 {path}`` with ``path`` interpolated into the shell
    string unquoted, so a spaced path word-split into four args and
    chmod failed with "No such file or directory", which surfaced as
    a ``CommandExitException`` that killed the session at startup.
    """
    assert E2B_API_KEY is not None
    org_id = f"acme-{TEST_RUN_ID}-spc"
    upstream = make_upstream_definition(
        id=f"e2e-spc-{TEST_RUN_ID}", command="uvx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "--from", "mcp",
        "python", "-c", _INLINE_MCP_SCRIPT,
    ]
    upstream.stdio.env = {}  # type: ignore[union-attr]

    file_repo = FileSandboxFileRepository(tmp_path)
    var_repo = FileTemplateVarRepository(tmp_path)
    expected_body = f"spaced-sandbox-file-{TEST_RUN_ID}"
    await file_repo.set(
        org_id, upstream.id, "SCREENSHOT",
        expected_body,
        "${HOME}/Screenshot 2026-05-06 at 10.51.07.png",
    )

    manager = UpstreamClientManager(
        upstreams=[upstream], org_id=org_id,
        template_var_repo=var_repo, sandbox_file_repo=file_repo,
    )
    materialize = await manager._resolve_sandbox_files(upstream)  # type: ignore[reportPrivateUsage]
    assert len(materialize) == 1
    expected_path = "/home/user/Screenshot 2026-05-06 at 10.51.07.png"
    assert materialize[0].target_path == expected_path
    assert materialize[0].contents == expected_body

    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-spc-{TEST_RUN_ID}",
        on_timeout_seconds=60,
    )
    try:
        async with service.session(
            session_id=f"e2e-spc-{TEST_RUN_ID}",
            org_id=org_id,
            upstream=upstream,
            resources=_make_resources(),
            denylist=(),
            materialize_files=materialize,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream,
                sandbox_session.write_stream,
            )
            async with session:
                # If chmod word-split the path, the session would die
                # before initialize() completes. Reaching the tool
                # call and reading the body back proves the spaced
                # file materialised at the right path.
                await asyncio.wait_for(session.initialize(), timeout=120.0)
                result = await asyncio.wait_for(
                    session.call_tool("read_file", {"path": expected_path}),
                    timeout=30.0,
                )
                assert result.content
                texts = [
                    c.text for c in result.content
                    if getattr(c, "type", None) == "text"
                ]
                assert any(expected_body in t for t in texts), (
                    f"spaced-path file body not in tool result: {texts!r}"
                )
    except E2BSDKError as exc:
        if _is_template_missing_error(exc):
            pytest.skip("mcpolis E2B templates not published on this account")
        raise
    except Exception as exc:
        if _is_inline_mcp_missing(exc):
            pytest.skip("mcp package not available in active python template")
        raise


@pytest.mark.asyncio
async def test_gcp_recipe_round_trips_path_and_body(
    tmp_path: Path,
) -> None:
    """The GCP recipe end-to-end: file ``GCP_CRED`` materialises at
    the resolved path, and ``GOOGLE_APPLICATION_CREDENTIALS`` (set
    to the same ``${HOME}/...`` literal as the file's target_path)
    arrives at the spawned process pointing at exactly that path.

    Operator types the path twice — once as the file's target_path,
    once as the env-var value — both with ``${HOME}`` so they stay
    portable. File names don't substitute; the path is the source
    of truth on both sides.
    """
    assert E2B_API_KEY is not None
    org_id = f"acme-{TEST_RUN_ID}-gcp"
    upstream = make_upstream_definition(
        id=f"e2e-gcp-{TEST_RUN_ID}", command="uvx",
    )
    upstream.stdio.args = [  # type: ignore[union-attr]
        "--from", "mcp",
        "python", "-c", _INLINE_MCP_SCRIPT,
    ]
    cred_path_template = "${HOME}/.config/gcloud/credentials.json"
    upstream.stdio.env = {  # type: ignore[union-attr]
        "GOOGLE_APPLICATION_CREDENTIALS": cred_path_template,
    }

    file_repo = FileSandboxFileRepository(tmp_path)
    var_repo = FileTemplateVarRepository(tmp_path)
    expected_body = f'{{"type":"service_account","run":"{TEST_RUN_ID}"}}'
    await file_repo.set(
        org_id, upstream.id, "GCP_CRED", expected_body,
        cred_path_template,
    )

    manager = UpstreamClientManager(
        upstreams=[upstream], org_id=org_id,
        template_var_repo=var_repo, sandbox_file_repo=file_repo,
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # type: ignore[reportPrivateUsage]
    materialize = await manager._resolve_sandbox_files(upstream)  # type: ignore[reportPrivateUsage]
    assert resolved.stdio is not None
    expected_path = "/home/user/.config/gcloud/credentials.json"
    assert resolved.stdio.env["GOOGLE_APPLICATION_CREDENTIALS"] == expected_path
    assert materialize[0].target_path == expected_path

    service = E2BSandboxService(
        RealE2BClient(api_key=E2B_API_KEY),
        mcpolis_instance=f"e2e-gcp-{TEST_RUN_ID}",
        on_timeout_seconds=60,
    )
    try:
        async with service.session(
            session_id=f"e2e-gcp-{TEST_RUN_ID}",
            org_id=org_id,
            upstream=resolved,
            resources=_make_resources(),
            denylist=(),
            materialize_files=materialize,
        ) as sandbox_session:
            session = ClientSession(
                sandbox_session.read_stream,
                sandbox_session.write_stream,
            )
            async with session:
                await asyncio.wait_for(session.initialize(), timeout=120.0)
                # The env var the MCP process inherits should be the
                # resolved absolute path.
                env_result = await asyncio.wait_for(
                    session.call_tool(
                        "read_env",
                        {"name": "GOOGLE_APPLICATION_CREDENTIALS"},
                    ),
                    timeout=30.0,
                )
                env_texts = [
                    c.text for c in env_result.content
                    if getattr(c, "type", None) == "text"
                ]
                assert any(expected_path in t for t in env_texts)
                # The file at that path should contain exactly what
                # we uploaded (path-and-body round trip).
                file_result = await asyncio.wait_for(
                    session.call_tool(
                        "read_file", {"path": expected_path},
                    ),
                    timeout=30.0,
                )
                file_texts = [
                    c.text for c in file_result.content
                    if getattr(c, "type", None) == "text"
                ]
                assert any(expected_body in t for t in file_texts)
    except E2BSDKError as exc:
        if _is_template_missing_error(exc):
            pytest.skip("mcpolis E2B templates not published on this account")
        raise
    except Exception as exc:
        if _is_inline_mcp_missing(exc):
            pytest.skip("mcp package not available in active python template")
        raise
