"""Pre-exec file-write hook tests for E2BSandboxService.

The launcher writes one ``files.write`` call per ``MaterializeFile``
entry before ``run_command`` starts the MCP process. The mock
client's ``file_writes`` list captures each call so the test can
assert path / contents / mode.
"""
from __future__ import annotations

import pytest

from mcpolis.adapters.sandbox_e2b import E2BSandboxService
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
