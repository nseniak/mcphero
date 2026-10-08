"""Which orgs list an address among their users, kept in memory.

``MongoConfigRepository`` answers ``find_user`` from it, so the
dashboard's ``/api/auth/me`` (every page load) finds a person's
invitations without reading every org's config. The repository feeds it
from the one place every config change goes through (its write, under
its lock), so no door that changes an org's users can leave it behind.
It is built from every org's config once, at the first lookup, and lives
as long as the process: one per process, like the repository's lock.
"""
from __future__ import annotations

from collections.abc import Mapping

from mcpolis.domain.model.email_address import email_key
from mcpolis.domain.model.settings import OrgUserEntry, UserDefinition


class UserIndex:
    """``email_key`` → the orgs whose users include that address."""

    def __init__(self) -> None:
        # True once every org's users were read in (``set_org`` for each).
        self.complete = False
        self._by_org: dict[str, dict[str, OrgUserEntry]] = {}
        self._by_address: dict[str, dict[str, OrgUserEntry]] = {}

    def set_org(self, org_id: str, users: Mapping[str, UserDefinition]) -> None:
        """Replace what the index knows of ``org_id``'s users."""
        self.drop_org(org_id)
        entries: dict[str, OrgUserEntry] = {}
        for email, user in users.items():
            # Two spellings of one address in one org: the first wins,
            # as an org has one entry per person.
            entries.setdefault(
                email_key(email),
                OrgUserEntry(org_id=org_id, email=email, user=user),
            )
        self._by_org[org_id] = entries
        for key, entry in entries.items():
            self._by_address.setdefault(key, {})[org_id] = entry

    def drop_org(self, org_id: str) -> None:
        """Forget ``org_id`` (its config was deleted)."""
        for key in self._by_org.pop(org_id, {}):
            orgs = self._by_address[key]
            del orgs[org_id]
            if not orgs:
                del self._by_address[key]

    def find(self, email: str) -> list[OrgUserEntry]:
        """The orgs whose users include ``email``, letter case ignored."""
        return list(self._by_address.get(email_key(email), {}).values())
