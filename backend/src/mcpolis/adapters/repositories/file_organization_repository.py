"""Single-org file-backed ``OrganizationRepository`` for standalone mode.

Standalone mode pretends the whole installation is one organization
named ``default`` — ``create_organization`` always raises because
multi-org is a cloud-mode feature. But memberships (the "has signed
in at least once" log that drives the Team tab's Pending/Joined
status) are persisted to ``<data_dir>/memberships.json`` so the same
``OrgService`` + ``list_users`` code paths work identically in both
modes.

The users dict of ``config.json`` remains the allowlist: who is
permitted to sign in and with what role. The membership file is the
sign-in log: one row per ``(org_id, email)`` that has actually
signed in.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from mcpolis.domain.model.subscription import PlanName, Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID, Membership, Organization
from mcpolis.domain.ports.organization_repository import OrganizationRepository

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class FileOrganizationRepository(OrganizationRepository):
    _DEFAULT_SLUG = "default"
    _DEFAULT_DISPLAY = "Default"

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "memberships.json"
        self._subscription_path = data_dir / "subscription.json"
        self._created_at = datetime.now(UTC)
        self._memberships: dict[tuple[str, str], Membership] = {}
        # Standalone is a self-hosted single-tenant install — the Free/Team
        # plan tiers are a property of the hosted mcphero.io offering, not
        # of the AGPL self-host. Default the lone org to the unlimited
        # (Team) plan so no upstream caps, seat caps, custom-role caps, or
        # sandbox-size restrictions apply, and the dashboard never shows
        # hosted-tier limits or upsells. (A persisted subscription.json
        # still wins, e.g. an explicit superadmin override.)
        self._subscription: Subscription = Subscription(plan=PlanName.team)
        self._load()
        self._load_subscription()

    def _default(self) -> Organization:
        return Organization(
            id=DEFAULT_ORG_ID,
            slug=self._DEFAULT_SLUG,
            display_name=self._DEFAULT_DISPLAY,
            created_at=self._created_at,
            created_by_email=None,
            subscription=self._subscription,
        )

    def _load_subscription(self) -> None:
        if not self._subscription_path.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self._subscription_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "organization.subscription.read.failed",
                path=str(self._subscription_path),
            )
            return
        try:
            self._subscription = Subscription.model_validate(raw)
        except (ValueError, TypeError):
            logger.warning(
                "organization.subscription.invalid",
                row=raw,
            )

    def _persist_subscription(self) -> None:
        tmp = self._subscription_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._subscription.model_dump(mode="json"), indent=2))
        tmp.replace(self._subscription_path)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw: list[dict[str, Any]] = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "organization.memberships.read.failed",
                path=str(self._path),
            )
            return
        for doc in raw:
            try:
                m = Membership(
                    org_id=doc["org_id"],
                    email=doc["email"],
                    role=doc["role"],
                    created_at=datetime.fromisoformat(doc["created_at"]),
                )
            except (KeyError, ValueError):
                logger.warning(
                    "organization.membership.invalid",
                    row=doc,
                )
                continue
            self._memberships[(m.org_id, m.email)] = m

    def _persist(self) -> None:
        data = [
            {
                "org_id": m.org_id,
                "email": m.email,
                "role": m.role,
                "created_at": m.created_at.isoformat(),
            }
            for m in self._memberships.values()
        ]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)

    # --- Organizations ---

    async def get_organization(self, org_id: str) -> Organization | None:
        if org_id != DEFAULT_ORG_ID:
            return None
        return self._default()

    async def get_by_slug(self, slug: str) -> Organization | None:
        if slug != self._DEFAULT_SLUG:
            return None
        return self._default()

    async def list_organizations(self) -> list[Organization]:
        return [self._default()]

    async def create_organization(
        self,
        slug: str,
        display_name: str,
        created_by_email: str | None = None,
    ) -> Organization:
        raise ValueError(
            "Creating additional organizations is not supported in "
            "standalone mode. Use MCPOLIS_MODE=cloud with a Mongo "
            "backend for multi-org."
        )

    # --- Memberships ---

    async def list_memberships(self, org_id: str) -> list[Membership]:
        return [
            m for m in self._memberships.values() if m.org_id == org_id
        ]

    async def get_memberships_for_email(self, email: str) -> list[Membership]:
        return [
            m for m in self._memberships.values() if m.email == email
        ]

    async def add_membership(
        self, org_id: str, email: str, role: str
    ) -> Membership:
        existing = self._memberships.get((org_id, email))
        created_at = existing.created_at if existing else datetime.now(UTC)
        membership = Membership(
            org_id=org_id, email=email, role=role, created_at=created_at,
        )
        self._memberships[(org_id, email)] = membership
        self._persist()
        return membership

    async def remove_membership(self, org_id: str, email: str) -> None:
        if self._memberships.pop((org_id, email), None) is not None:
            self._persist()

    async def delete_organization(self, org_id: str) -> None:
        raise ValueError("Cannot delete organizations in standalone mode")

    async def update_subscription(
        self, org_id: str, subscription: Subscription,
    ) -> None:
        if org_id != DEFAULT_ORG_ID:
            raise ValueError(
                "Standalone mode has only the default organization",
            )
        self._subscription = subscription
        self._persist_subscription()

    async def ensure_default_org(self) -> Organization:
        return self._default()
