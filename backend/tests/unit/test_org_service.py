"""Tests for the OrgService domain service (Phase 3)."""
from __future__ import annotations

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_organization_repository import (
    FileOrganizationRepository,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.org_service import (
    OrgService,
    SlugValidationError,
    validate_slug,
    suggest_slug,
)


# --- Slug helpers ---


def test_validate_slug_good() -> None:
    for slug in ["acme", "my-team", "abc123", "org", "a-1"]:
        validate_slug(slug)


def test_validate_slug_too_short() -> None:
    with pytest.raises(SlugValidationError):
        validate_slug("ab")


def test_validate_slug_too_long() -> None:
    with pytest.raises(SlugValidationError):
        validate_slug("a" * 41)


def test_validate_slug_leading_hyphen() -> None:
    with pytest.raises(SlugValidationError):
        validate_slug("-bad")


def test_validate_slug_trailing_hyphen() -> None:
    with pytest.raises(SlugValidationError):
        validate_slug("bad-")


def test_validate_slug_uppercase() -> None:
    with pytest.raises(SlugValidationError):
        validate_slug("Bad")


def test_validate_slug_reserved() -> None:
    for reserved in ["admin", "mcp", "api", "system", "default"]:
        with pytest.raises(SlugValidationError, match="reserved"):
            validate_slug(reserved)


def test_suggest_slug() -> None:
    assert suggest_slug("Acme Corp") == "acme-corp"
    assert suggest_slug("  Spaces  ") == "spaces"
    assert suggest_slug("a!b@c") == "a-b-c"
    assert suggest_slug("") == "org"


# --- OrgService with File backend (standalone mode) ---


def make_org_service_file(tmp_path: object) -> OrgService:
    from pathlib import Path

    p = Path(str(tmp_path))
    config_path = p / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_store = FileConfigStore(config_path)
    config_store.ensure_defaults_sync(DEFAULT_ORG_ID)
    data_dir = p / "data"
    org_repo = FileOrganizationRepository(data_dir)
    return OrgService(org_repo=org_repo, config_repo=config_store)


@pytest.mark.asyncio
async def test_file_mode_create_org_raises(tmp_path: object) -> None:
    svc = make_org_service_file(tmp_path)
    with pytest.raises(ValueError, match="standalone"):
        await svc.create_organization(
            slug="neworg",
            display_name="New Org",
            creator_email="alice@x.com",
        )


@pytest.mark.asyncio
async def test_file_mode_list_user_orgs_returns_default(
    tmp_path: object,
) -> None:
    svc = make_org_service_file(tmp_path)
    # The user must exist in config to get the default org.
    from mcpolis.domain.model.settings import UserDefinition

    await svc._config_repo.set_user(
        DEFAULT_ORG_ID, "alice@x.com", UserDefinition(role="admin"),
    )
    orgs = await svc.list_user_orgs("alice@x.com")
    assert len(orgs) == 1
    assert orgs[0].slug == "default"


@pytest.mark.asyncio
async def test_file_mode_list_user_orgs_empty_for_unknown(
    tmp_path: object,
) -> None:
    svc = make_org_service_file(tmp_path)
    orgs = await svc.list_user_orgs("nobody@x.com")
    assert orgs == []



# --- OrgService with Mongo backend (cloud mode) ---

from tests.unit.mongo_fixture import mongo_available, temp_mongo_database

_mongo_mark = pytest.mark.skipif(
    not mongo_available(), reason="Mongo not reachable"
)


@pytest.fixture
async def mongo_org_service():
    """Create an OrgService backed by a throwaway Mongo database."""
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

    async with temp_mongo_database() as db:
        org_repo = MongoOrganizationRepository(db)
        config_repo = MongoConfigRepository(
            OrgScopedCollection(db[COLL_CONFIG], COLL_CONFIG),
        )
        yield OrgService(org_repo=org_repo, config_repo=config_repo)


@_mongo_mark
@pytest.mark.asyncio
async def test_create_org_makes_creator_admin(
    mongo_org_service: OrgService,
) -> None:
    svc = mongo_org_service
    org = await svc.create_organization(
        slug="acme",
        display_name="Acme Corp",
        creator_email="alice@x.com",
    )
    assert org.slug == "acme"
    assert org.display_name == "Acme Corp"

    # Creator should be admin
    role = await svc.get_user_role(org.id, "alice@x.com")
    assert role == "admin"

    # Membership should exist
    assert await svc.is_member(org.id, "alice@x.com")


@_mongo_mark
@pytest.mark.asyncio
async def test_create_org_seeds_default_roles(
    mongo_org_service: OrgService,
) -> None:
    svc = mongo_org_service
    org = await svc.create_organization(
        slug="beta",
        display_name="Beta",
        creator_email="bob@x.com",
    )
    config = await svc._config_repo.load(org.id)
    assert "admin" in config.roles


@_mongo_mark
@pytest.mark.asyncio
async def test_slug_collision_rejected(
    mongo_org_service: OrgService,
) -> None:
    svc = mongo_org_service
    await svc.create_organization(
        slug="taken", display_name="First", creator_email="a@x.com",
    )
    with pytest.raises(ValueError, match="already taken"):
        await svc.create_organization(
            slug="taken", display_name="Second", creator_email="b@x.com",
        )


@_mongo_mark
@pytest.mark.asyncio
async def test_mongo_add_membership_preserves_created_at_on_role_update() -> None:
    from mcpolis.adapters.repositories.mongo_organization_repository import (
        MongoOrganizationRepository,
    )

    async with temp_mongo_database() as db:
        repo = MongoOrganizationRepository(db)
        first = await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "admin")
        second = await repo.add_membership(DEFAULT_ORG_ID, "alice@x.com", "default")

        rows = await repo.list_memberships(DEFAULT_ORG_ID)
        assert len(rows) == 1
        assert rows[0].role == "default"
        assert second.created_at == first.created_at


@_mongo_mark
@pytest.mark.asyncio
async def test_multi_org_user_can_list(
    mongo_org_service: OrgService,
) -> None:
    svc = mongo_org_service
    await svc.create_organization(
        slug="org-a", display_name="A", creator_email="multi@x.com",
    )
    await svc.create_organization(
        slug="org-b", display_name="B", creator_email="multi@x.com",
    )
    orgs = await svc.list_user_orgs("multi@x.com")
    slugs = {o.slug for o in orgs}
    assert "org-a" in slugs
    assert "org-b" in slugs
