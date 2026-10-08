"""Cloud startup must not turn invitations into memberships.

A membership row means "accepted the invitation": the Team page shows
"pending" without one, and the last-admin rule counts only addresses
with one. The cloud startup step used to create a row for every address
in the org's user list, so after any restart an invited admin who never
joined counted as a real admin again.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_config_repository import (
    MongoConfigRepository,
)
from mcpolis.adapters.repositories.mongo_organization_repository import (
    MongoOrganizationRepository,
)
from mcpolis.domain.model.settings import UserDefinition
from mcpolis.entrypoints.storage_factory import StorageBundle, initialize_storage
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database


@pytest.mark.asyncio
async def test_restart_does_not_turn_an_invitation_into_a_member() -> None:
    if not mongo_available():
        pytest.skip("Mongo not reachable (set MCPOLIS_TEST_MONGO_URI)")
    async with temp_mongo_database() as db:
        org_repo = MongoOrganizationRepository(db)
        config_repo = MongoConfigRepository(OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG))
        org = await org_repo.create_organization("acme", "Acme")
        await config_repo.ensure_defaults(org.id)
        await config_repo.set_user(org.id, "alice@x.com", UserDefinition(role="admin"))
        await org_repo.add_membership(org.id, "alice@x.com", "admin")
        await config_repo.set_user(org.id, "invited@x.com", UserDefinition(role="admin"))
        bundle = SimpleNamespace(
            mongo=SimpleNamespace(database=db),
            organization_repo=org_repo,
            config_repo=config_repo,
        )

        await initialize_storage(cast(StorageBundle, bundle))

        members = {m.email for m in await org_repo.list_memberships(org.id)}
        assert members == {"alice@x.com"}
