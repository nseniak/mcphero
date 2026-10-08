"""An org deletion runs to its end once started, from the operator MCP
too, and revokes the org's service tokens before its slow steps.

``delete_organization`` deletes the org document first, then tears the
runtime down, purges every org-scoped collection and kills the org's
sandboxes last. The operator MCP (``/admin-mcp/system``) had no ``tools/call``
wrapper and the action no ``@runs_to_completion``: an AI client that
cancelled the call half-way left the org gone ("not found", so no retry)
while its service tokens, which bypass membership, kept working and could
no longer be revoked. And the tokens were purged last, after an E2B kill
per sandbox, so any stop in between (a crash, a deploy's cut) left them.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path

import pytest
import structlog

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.inmemory_sandbox_persistence_repository import (
    InMemorySandboxPersistenceRepository,
)
from mcpolis.domain.ports.organization_repository import Membership
from mcpolis.domain.services.background_tasks import drain_every_set
from mcpolis.domain.services.org_runtime import OrgRuntime
from mcpolis.domain.services.org_service import OrgService
from mcpolis.entrypoints.controllers.superadmin_controller import (
    create_superadmin_mcp_server,
)
from tests.unit.factories import (
    Gate,
    cancel_mcp_call_while_gated,
    cancel_natively_while_gated,
    cancel_while_gated,
    make_full_access_config,
    make_service_token_record,
)
from tests.unit.test_org_deletion_sandboxes import make_ref
from tests.unit.test_superadmin_audit import InMemoryOrgRepo, make_org

OPERATOR = "op@mcphero.io"

# ``cancel_while_gated`` or ``cancel_natively_while_gated``.
CancelWhileGated = Callable[
    [Gate, Callable[[], Coroutine[object, object, object]]], Awaitable[None],
]


class DeletableOrgRepo(InMemoryOrgRepo):
    async def delete_organization(self, org_id: str) -> None:
        self._orgs.pop(org_id, None)
        self._memberships = [
            m for m in self._memberships if m.org_id != org_id
        ]


class TokenPurgeWaits(FileServiceTokenRepository):
    """The service-token purge waits at ``gate`` before it deletes."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.gate = Gate()

    async def delete_for_org(self, org_id: str) -> int:
        await self.gate.hold()
        return await super().delete_for_org(org_id)


class SandboxKillWaits:
    """A sandbox provider whose kill of a persisted sandbox waits at
    ``gate``, as an E2B call can for its whole timeout."""

    def __init__(self) -> None:
        self.gate = Gate()
        self.killed: list[str] = []

    async def kill_persisted_session(self, *, org_id: str, upstream_id: str) -> None:
        del org_id
        await self.gate.hold()
        self.killed.append(upstream_id)

    async def on_upstream_removed(self, *, org_id: str, upstream_id: str) -> bool:
        del org_id, upstream_id
        return False


class NoRuntimes:
    def get_cached(self, org_id: str) -> OrgRuntime | None:
        del org_id
        return None


def make_org_repo() -> DeletableOrgRepo:
    return DeletableOrgRepo(
        [make_org("acme")],
        [Membership(org_id="acme", email="a@acme.com", role="admin")],
    )


async def make_tokens(tokens: FileServiceTokenRepository) -> None:
    await tokens.create(make_service_token_record(label="ci-bot", org_id="acme"))


def make_org_service(
    tmp_path: Path,
    org_repo: DeletableOrgRepo,
    tokens: FileServiceTokenRepository,
) -> OrgService:
    return OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=FileConfigStore(tmp_path / "config.json"),
        service_token_repo=tokens,
    )


async def test_a_cancelled_operator_delete_still_finishes(tmp_path: Path) -> None:
    """Cancelled over the protocol while the token purge waits: the
    tokens are still purged, and the log line that names the operator,
    the deletion's only record, is still written."""
    org_repo = make_org_repo()
    tokens = TokenPurgeWaits(tmp_path)
    await make_tokens(tokens)
    server = create_superadmin_mcp_server(
        org_repo,  # type: ignore[arg-type]
        NoRuntimes(),  # type: ignore[arg-type]
        make_org_service(tmp_path, org_repo, tokens),
        current_operator=lambda: OPERATOR,
    )

    with structlog.testing.capture_logs() as logs:
        await cancel_mcp_call_while_gated(
            server, tokens.gate,
            "delete_organization", {"slug": "acme", "confirm": True},
            caller=OPERATOR,
        )

    assert await org_repo.get_by_slug("acme") is None
    assert await tokens.list_for_org("acme") == [], (
        "the org is gone but its service token survived the cancelled delete"
    )
    assert [
        line["actor"] for line in logs
        if line["event"] == "superadmin.organization.deleted"
    ] == [OPERATOR]


@pytest.mark.parametrize(
    "cancel", [cancel_while_gated, cancel_natively_while_gated],
)
async def test_a_cancelled_deletion_still_purges_the_service_tokens(
    tmp_path: Path, cancel: CancelWhileGated,
) -> None:
    """Whichever door calls it, cancelled by an anyio scope or natively."""
    org_repo = make_org_repo()
    tokens = TokenPurgeWaits(tmp_path)
    await make_tokens(tokens)
    org_service = make_org_service(tmp_path, org_repo, tokens)

    await cancel(tokens.gate, lambda: org_service.delete_organization("acme"))

    assert await tokens.list_for_org("acme") == []


async def test_the_service_tokens_go_before_the_sandboxes_are_released(
    tmp_path: Path,
) -> None:
    """Killing the org's sandboxes waits on E2B; the tokens are already
    revoked by then."""
    org_repo = make_org_repo()
    tokens = FileServiceTokenRepository(tmp_path)
    await make_tokens(tokens)
    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(make_ref(
        org_id="acme", upstream_id="u1", sandbox_id="sbx-1", volume_id=None,
    ))
    sandboxes = SandboxKillWaits()
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=FileConfigStore(tmp_path / "config.json"),
        service_token_repo=tokens,
        sandbox_persistence_repo=persistence,
        sandbox_services={"e2b": sandboxes},  # type: ignore[dict-item]
    )

    deleting = asyncio.create_task(org_service.delete_organization("acme"))
    await asyncio.wait_for(sandboxes.gate.reached.wait(), 5)
    tokens_while_releasing = await tokens.list_for_org("acme")
    sandboxes.gate.release.set()
    await asyncio.wait_for(deleting, 5)

    assert tokens_while_releasing == [], (
        "the org's service tokens still worked while its sandboxes were killed"
    )
    assert sandboxes.killed == ["u1"]


async def test_a_deletion_the_shutdown_cuts_at_the_sandboxes_leaves_no_secrets(
    tmp_path: Path,
) -> None:
    """A deploy's shutdown waits for a running deletion only so long (its
    job drain), then cancels it. Cut while an E2B kill hung, it left the
    org's config and Variables behind, and the org was gone: nothing could
    retry it. The other collections now go before the sandboxes."""
    org_repo = make_org_repo()
    config = FileConfigStore(tmp_path / "config.json")
    await config.save("acme", make_full_access_config(["u1"], ["a@acme.com"]))
    variables = FileTemplateVarRepository(tmp_path)
    await variables.set("acme", "u1", "API_KEY", "secret-value", is_secret=True)
    persistence = InMemorySandboxPersistenceRepository()
    await persistence.upsert(make_ref(
        org_id="acme", upstream_id="u1", sandbox_id="sbx-1", volume_id=None,
    ))
    sandboxes = SandboxKillWaits()
    org_service = OrgService(
        org_repo=org_repo,  # type: ignore[arg-type]
        config_repo=config,
        template_var_repo=variables,
        sandbox_persistence_repo=persistence,
        sandbox_services={"e2b": sandboxes},  # type: ignore[dict-item]
    )

    deleting = asyncio.create_task(org_service.delete_organization("acme"))
    await asyncio.wait_for(sandboxes.gate.reached.wait(), 5)
    # The shutdown's job drain, over: what still runs is cancelled.
    await drain_every_set(0, unwind_timeout=1)
    await asyncio.gather(deleting, return_exceptions=True)

    assert sandboxes.killed == [], "the hung kill was meant to be cut"
    assert await variables.list_summaries("acme", "u1") == []
    assert not (await config.load("acme")).users
