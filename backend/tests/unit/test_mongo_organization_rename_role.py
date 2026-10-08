"""Mongo side of the membership role rename (see the file-store test in
``test_file_organization_repository.py``)."""
from __future__ import annotations

import pytest

from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)

from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


@pytest.mark.skipif(not mongo_available(), reason="Mongo not reachable")
@pytest.mark.asyncio
async def test_mongo_rename_role_moves_org_memberships_of_that_role_only() -> None:
    async with temp_mongo_database() as db:
        repo = MongoOrganizationRepository(db)
        await repo.add_membership("org-a", "alice@x.com", "reader")
        await repo.add_membership("org-a", "bob@x.com", "admin")
        await repo.add_membership("org-b", "carol@x.com", "reader")

        assert await repo.rename_role("org-a", "reader", "auditor") == 1

        org_a = {m.email: m.role for m in await repo.list_memberships("org-a")}
        assert org_a == {"alice@x.com": "auditor", "bob@x.com": "admin"}
        org_b = {m.email: m.role for m in await repo.list_memberships("org-b")}
        assert org_b == {"carol@x.com": "reader"}
