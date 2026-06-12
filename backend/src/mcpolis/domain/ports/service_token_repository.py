"""Port for the service-token registry.

Two implementations:

- :class:`mcpolis.adapters.repositories.file_service_token_repository.FileServiceTokenRepository`
  for standalone mode (JSON on disk).
- :class:`mcpolis.adapters.repositories.mongo_service_token_repository.MongoServiceTokenRepository`
  for cloud mode.

Nothing stored is secret — ``token_hash`` is preimage-resistant over a
256-bit random input, and the metadata (label, role, creator) is the
same sensitivity class as ``config.users`` — so neither implementation
encrypts at rest.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mcpolis.domain.model.service_token import ServiceTokenRecord


class DuplicateServiceTokenLabelError(Exception):
    """A token with this (org_id, label) already exists."""


class ServiceTokenRepository(Protocol):
    async def create(self, record: ServiceTokenRecord) -> None:
        """Persist a new token record.

        Raises :class:`DuplicateServiceTokenLabelError` when the
        (org_id, label) pair is already taken — the label is the
        token's audit identity within the org and must be unambiguous.
        """
        ...

    async def get_by_hash(self, token_hash: str) -> ServiceTokenRecord | None:
        """Look up a token by its sha256 hash.

        Deliberately **global** (no org filter): this runs on the auth
        path, before any org context exists — the record itself tells
        the verifier which org the token is pinned to.
        """
        ...

    async def list_for_org(self, org_id: str) -> list[ServiceTokenRecord]:
        """All tokens for one org, sorted by label ascending."""
        ...

    async def get_by_label(
        self, org_id: str, label: str
    ) -> ServiceTokenRecord | None:
        ...

    async def delete_by_label(self, org_id: str, label: str) -> bool:
        """Revoke a token. Returns False when the label is unknown."""
        ...

    async def delete_for_org(self, org_id: str) -> int:
        """Revoke every token of an org (org-deletion cascade).

        Without this, a deleted org's tokens would stay valid forever
        and be unrevocable (the org's dashboard is gone). Returns the
        number of tokens removed.
        """
        ...

    async def touch_last_used(self, token_hash: str, when: datetime) -> None:
        """Update ``last_used_at``. Missing token → no-op (it may have
        been revoked between verify and touch)."""
        ...
