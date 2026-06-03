"""Mongo-backed ``OrganizationRepository`` for cloud mode."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from mcpolis.adapters.repositories.mongo_client import (
    COLL_MEMBERSHIPS,
    COLL_ORGANIZATIONS,
    MotorDatabase,
)
from mcpolis.domain.model.subscription import Subscription
from mcpolis.domain.ports import DEFAULT_ORG_ID, Membership, Organization
from mcpolis.domain.ports.organization_repository import OrganizationRepository


class MongoOrganizationRepository(OrganizationRepository):
    """Organizations are intentionally *not* scoped by org_id — the
    collection IS the org index. Memberships are org-scoped.

    This class is the only Mongo repository that touches the raw motor
    collections because of that org-less lookup pattern. The
    architectural grep test allow-lists it explicitly.
    """

    def __init__(self, db: MotorDatabase) -> None:
        self._orgs = db[COLL_ORGANIZATIONS]
        self._memberships = db[COLL_MEMBERSHIPS]

    # --- Organizations ---

    @staticmethod
    def _from_doc(doc: dict[str, Any]) -> Organization:
        created_at = doc["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        # Subscription is optional in stored docs (older orgs predate
        # the field); default to free when absent so /api/me always
        # has a plan to render.
        subscription_doc: dict[str, Any] = doc.get("subscription") or {}
        subscription = Subscription.model_validate(subscription_doc)
        return Organization(
            id=doc["id"],
            slug=doc["slug"],
            display_name=doc["display_name"],
            created_at=created_at,
            created_by_email=doc.get("created_by_email"),
            subscription=subscription,
        )

    async def get_organization(self, org_id: str) -> Organization | None:
        doc = await self._orgs.find_one({"id": org_id})
        return self._from_doc(doc) if doc else None

    async def get_by_slug(self, slug: str) -> Organization | None:
        doc = await self._orgs.find_one({"slug": slug})
        return self._from_doc(doc) if doc else None

    async def list_organizations(self) -> list[Organization]:
        cursor = self._orgs.find({})
        docs: list[dict[str, Any]] = await cursor.to_list(length=None)
        return [self._from_doc(d) for d in docs]

    async def create_organization(
        self,
        slug: str,
        display_name: str,
        created_by_email: str | None = None,
    ) -> Organization:
        existing = await self._orgs.find_one({"slug": slug})
        if existing is not None:
            raise ValueError(f"Organization slug '{slug}' is already taken")
        org = Organization(
            id=uuid.uuid4().hex,
            slug=slug,
            display_name=display_name,
            created_at=datetime.now(UTC),
            created_by_email=created_by_email,
        )
        await self._orgs.insert_one(
            {
                "id": org.id,
                "slug": org.slug,
                "display_name": org.display_name,
                "created_at": org.created_at.isoformat(),
                "created_by_email": org.created_by_email,
                "subscription": org.subscription.model_dump(mode="json"),
            }
        )
        return org

    async def ensure_default_org(self) -> Organization:
        existing = await self._orgs.find_one({"id": DEFAULT_ORG_ID})
        if existing is not None:
            return self._from_doc(existing)
        org = Organization(
            id=DEFAULT_ORG_ID,
            slug="default",
            display_name="Default",
            created_at=datetime.now(UTC),
        )
        await self._orgs.insert_one(
            {
                "id": org.id,
                "slug": org.slug,
                "display_name": org.display_name,
                "created_at": org.created_at.isoformat(),
                "created_by_email": None,
                "subscription": org.subscription.model_dump(mode="json"),
            }
        )
        return org

    async def update_subscription(
        self, org_id: str, subscription: Subscription,
    ) -> None:
        await self._orgs.update_one(
            {"id": org_id},
            {"$set": {"subscription": subscription.model_dump(mode="json")}},
        )

    # --- Memberships ---

    @staticmethod
    def _membership_from_doc(doc: dict[str, Any]) -> Membership:
        created_at = doc.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif not isinstance(created_at, datetime):
            created_at = datetime.now(UTC)
        return Membership(
            org_id=doc["org_id"],
            email=doc["email"],
            role=doc["role"],
            created_at=created_at,
        )

    async def list_memberships(self, org_id: str) -> list[Membership]:
        cursor = self._memberships.find({"org_id": org_id})
        docs: list[dict[str, Any]] = await cursor.to_list(length=None)
        return [self._membership_from_doc(d) for d in docs]

    async def get_memberships_for_email(self, email: str) -> list[Membership]:
        cursor = self._memberships.find({"email": email})
        docs: list[dict[str, Any]] = await cursor.to_list(length=None)
        return [self._membership_from_doc(d) for d in docs]

    async def add_membership(
        self, org_id: str, email: str, role: str
    ) -> Membership:
        # Upsert preserving ``created_at`` on existing rows so a role
        # change doesn't reset the "joined at" timestamp.
        now = datetime.now(UTC)
        doc = await self._memberships.find_one_and_update(
            {"org_id": org_id, "email": email},
            {
                "$set": {"role": role},
                "$setOnInsert": {
                    "org_id": org_id,
                    "email": email,
                    "created_at": now.isoformat(),
                },
            },
            upsert=True,
            return_document=True,
        )
        return self._membership_from_doc(doc)

    async def remove_membership(self, org_id: str, email: str) -> None:
        await self._memberships.delete_one({"org_id": org_id, "email": email})

    async def delete_organization(self, org_id: str) -> None:
        await self._memberships.delete_many({"org_id": org_id})
        await self._orgs.delete_one({"id": org_id})
