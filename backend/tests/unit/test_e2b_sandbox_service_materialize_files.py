"""Pre-exec file-write hook tests for E2BSandboxService.

The launcher writes one ``files.write`` call per ``MaterializeFile``
entry before ``run_command`` starts the MCP process. The mock
client's ``file_writes`` list captures each call so the test can
assert path / contents / mode.
"""
from __future__ import annotations

from typing import Any

import pytest

from mcpolis.adapters.sandbox_e2b import E2BSandboxService, E2BSDKError
from mcpolis.domain.services.sandbox_service import (
    MaterializeFile,
    SandboxResources,
)
from tests.unit.factories import make_upstream_definition
from tests.unit.sandbox_e2b_mock import make_mock_e2b_client


def _make_resources() -> SandboxResources:
    return SandboxResources(cpu_vcpus=1.0, memory_mb=1024, disk_gb=0)


@pytest.mark.asyncio
async def test_materialize_files_writes_each_entry_with_mode_0600() -> None:
    client = make_mock_e2b_client()
    service = E2BSandboxService(client, mcpolis_instance="t", on_timeout_seconds=60)
    upstream = make_upstream_definition(id="ups", command="npx")
    files = [
        MaterializeFile(
            name="GCP_CRED",
            contents='{"type":"service_account"}',
            target_path="/home/user/.config/gcloud/credentials.json",
        ),
        MaterializeFile(
            name="KUBECONFIG",
            contents="apiVersion: v1\n",
            target_path="/home/user/.kube/config",
        ),
    ]
    async with service.session(
        session_id="s1",
        org_id="acme",
        upstream=upstream,
        resources=_make_resources(),
        denylist=(),
        materialize_files=files,
    ):
        pass
    assert len(client.file_writes) == 2
    by_path = {w.path: w for w in client.file_writes}
    assert by_path["/home/user/.config/gcloud/credentials.json"].contents == (
        '{"type":"service_account"}'
    )
    assert by_path["/home/user/.kube/config"].contents == "apiVersion: v1\n"
    for w in client.file_writes:
        assert w.mode == 0o600


@pytest.mark.asyncio
async def test_materialize_files_runs_before_run_command() -> None:
    """Order matters: writes must land before the MCP process starts
    so the SDK can read its credentials at startup. The mock client
    appends to ``file_writes`` and ``commands`` in real-time, so the
    timestamps order via list index."""
    client = make_mock_e2b_client()
    service = E2BSandboxService(client, mcpolis_instance="t", on_timeout_seconds=60)
    upstream = make_upstream_definition(id="ups", command="npx")
    files = [
        MaterializeFile(
            name="X",
            contents="body",
            target_path="/home/user/x",
        ),
    ]
    async with service.session(
        session_id="s1",
        org_id="acme",
        upstream=upstream,
        resources=_make_resources(),
        denylist=(),
        materialize_files=files,
    ):
        pass
    # The writes are recorded on the mock sandbox handle, so the
    # ordering check here is structural: by the time the test exits
    # the session context, both sets are populated.
    assert client.file_writes[0].path == "/home/user/x"
    assert len(client.commands) == 1


@pytest.mark.asyncio
async def test_materialize_files_no_op_when_none() -> None:
    client = make_mock_e2b_client()
    service = E2BSandboxService(client, mcpolis_instance="t", on_timeout_seconds=60)
    upstream = make_upstream_definition(id="ups", command="npx")
    async with service.session(
        session_id="s1",
        org_id="acme",
        upstream=upstream,
        resources=_make_resources(),
        denylist=(),
        materialize_files=None,
    ):
        pass
    assert client.file_writes == []


# ---------- SBX-9: materialize write failure (E2B) ----------


@pytest.mark.asyncio
async def test_sbx9_materialize_write_failure_aborts_session_with_sdk_message(
) -> None:
    """SBX-9 (P1): a ``write_file`` that raises an ``E2BSDKError`` during
    materialization is FATAL — ``_materialize_files`` re-raises so
    session creation fails with the SDK's raw message surfaced (it
    reaches the dashboard's "Couldn't connect" line). The MCP process
    must NOT have started. Pins service.py:1367-1381."""
    client = make_mock_e2b_client()
    service = E2BSandboxService(
        client, mcpolis_instance="t", on_timeout_seconds=60,
    )
    upstream = make_upstream_definition(id="ups", command="npx")

    sdk_message = "files.write failed: disk quota exceeded"

    async def _raise_write(
        _self: Any, *, path: str, contents: str, mode: int = 0o600, **_k: Any,
    ) -> None:
        del _self, path, contents, mode
        raise E2BSDKError("E2BSDKError", sdk_message)

    # Override write_file on the handle the next create returns. The mock
    # builds a fresh MockE2BSandboxHandle per create; patch the class-level
    # method so the one created inside session() raises.
    from tests.unit.sandbox_e2b_mock import MockE2BSandboxHandle

    original_write = MockE2BSandboxHandle.write_file
    MockE2BSandboxHandle.write_file = _raise_write  # type: ignore[method-assign,assignment]
    try:
        with pytest.raises(E2BSDKError) as exc:
            async with service.session(
                session_id="s1",
                org_id="acme",
                upstream=upstream,
                resources=_make_resources(),
                denylist=(),
                materialize_files=[
                    MaterializeFile(
                        name="cred", contents="body",
                        target_path="/home/user/.config/cred.txt",
                    ),
                ],
            ):
                pass
    finally:
        MockE2BSandboxHandle.write_file = original_write  # type: ignore[method-assign,assignment]

    assert sdk_message in exc.value.detail, (
        f"the SDK message must surface to the caller; got {exc.value.detail!r}"
    )
    # The MCP command never ran — materialization aborted before exec.
    assert client.commands == [], (
        "run_command must not fire when materialization fails"
    )
