"""A ``UserAdminService`` (the teammate actions both admin doors share)
over real file stores, with the gateway and the upstream side recorded.

The org it acts on is ``default``. A person can also hold membership
rows in other orgs (``globex`` below): the file organization store keeps
rows for any org id, which is all the teammate actions read about other
orgs. ``steps`` lists what a removal tore down, in order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_audit_repository import FileAuditRepository
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import FileConnectionStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.admin_actions import AdminActionDeps
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.user_admin_service import UserAdminService
from tests.unit.factories import make_runtime_manager

ADMIN = "admin@example.com"
OTHER_ORG = "globex"


class RecordingConnectionStore(FileConnectionStore):
    """Records each per-user purge in ``steps``."""

    def __init__(self, data_dir: Path, steps: list[str]) -> None:
        super().__init__(data_dir)
        self.steps = steps

    async def delete_all_for_user(self, org_id: str, user_id: str) -> int:
        self.steps.append(f"delete_sign_ins:{org_id}:{user_id}")
        return await super().delete_all_for_user(org_id, user_id)


class RecordingClientManager(UpstreamClientManager):
    """Records each "close every upstream session of a user" in ``steps``."""

    def __init__(self, steps: list[str]) -> None:
        super().__init__([])
        self.steps = steps

    async def disconnect_all_user_sessions(self, user_id: str) -> int:
        self.steps.append(f"close_upstream_sessions:{user_id}")
        return await super().disconnect_all_user_sessions(user_id)


@dataclass
class UserAdminParts:
    service: UserAdminService
    runtime_manager: OrgRuntimeManager
    org_repo: FileOrganizationRepository
    audit_repo: FileAuditRepository
    connection_store: RecordingConnectionStore
    coordinator: PendingAuthCoordinator
    steps: list[str] = field(default_factory=list[str])
    revoked: list[str] = field(default_factory=list[str])


def make_config_file(tmp_path: Path, users: dict[str, str]) -> Path:
    """``users``: email → role; roles ``admin`` (admin) and ``user``."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "upstreams": {},
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {email: {"role": role} for email, role in users.items()},
    }))
    return path


async def make_user_admin(
    tmp_path: Path,
    *,
    users: dict[str, str],
    memberships: list[tuple[str, str, str]],
    org_repo: FileOrganizationRepository | None = None,
    config_store: FileConfigStore | None = None,
) -> UserAdminParts:
    """The ``default`` org has ``users`` (email → role) in its config;
    ``memberships`` are the rows (org id, email, role): the invitations
    accepted. A user of ``default`` with no row is a pending invitation.

    A given ``org_repo`` must be built on ``tmp_path / "data"``, a given
    ``config_store`` on ``tmp_path / "config.json"`` (a gated one, say)."""
    steps: list[str] = []
    org_repo = org_repo or FileOrganizationRepository(tmp_path / "data")
    for org_id, email, role in memberships:
        await org_repo.add_membership(org_id, email, role)
    config_path = make_config_file(tmp_path, users)
    config_store = config_store or FileConfigStore(config_path)
    policy = PolicyEngine(
        config_store.ensure_defaults_sync(DEFAULT_ORG_ID),
        [email for org_id, email, _ in memberships if org_id == DEFAULT_ORG_ID],
    )
    runtime_manager = make_runtime_manager(
        policy, client_manager=RecordingClientManager(steps),
    )
    audit_repo = FileAuditRepository(tmp_path / "data" / "audit.jsonl")
    connection_store = RecordingConnectionStore(tmp_path / "data", steps)
    coordinator = PendingAuthCoordinator(b"k" * 32)
    revoked: list[str] = []

    def revoke_gateway_user(email: str) -> int:
        steps.append(f"revoke_gateway_sign_in:{email}")
        revoked.append(email)
        return 2

    async def terminate_gateway_sessions(org_id: str, email: str) -> int:
        steps.append(f"close_gateway_sessions:{org_id}:{email}")
        return 1

    service = UserAdminService(AdminActionDeps(
        runtime_manager=runtime_manager,
        policy_store=config_store,
        audit_repo=audit_repo,
        connection_store=connection_store,
        auth_coordinator=coordinator,
        server_url="http://localhost:8000",
        event_bus=None,
        org_repo=org_repo,
        allow_stdio_mcp=True,
        revoke_gateway_user=revoke_gateway_user,
        terminate_gateway_sessions=terminate_gateway_sessions,
    ))
    return UserAdminParts(
        service=service,
        runtime_manager=runtime_manager,
        org_repo=org_repo,
        audit_repo=audit_repo,
        connection_store=connection_store,
        coordinator=coordinator,
        steps=steps,
        revoked=revoked,
    )


async def membership_orgs(parts: UserAdminParts, email: str) -> set[str]:
    rows = await parts.org_repo.get_memberships_for_email(email)
    return {row.org_id for row in rows}
