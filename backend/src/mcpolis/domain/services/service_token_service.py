"""Service-token lifecycle: mint, list, revoke, verify.

The service owns the only code path that ever sees a raw token after
mint time: ``verify`` re-hashes the presented bearer and looks the
hash up in the registry. ``last_used_at`` writes are throttled to at
most one per :data:`LAST_USED_WRITE_INTERVAL_SECONDS` per token so the
hot verify path doesn't turn every gateway request into a registry
write. The throttle state is in-memory — good enough for the
single-backend deployment; a second instance would just write a bit
more often.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from mcpolis.domain.model.service_token import (
    SERVICE_TOKEN_PREFIX,
    ServiceTokenRecord,
    generate_service_token,
    hash_service_token,
)
from mcpolis.domain.ports.service_token_repository import (
    ServiceTokenRepository,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

LAST_USED_WRITE_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class MintedServiceToken:
    raw_token: str
    record: ServiceTokenRecord


@dataclass
class ServiceTokenService:
    repo: ServiceTokenRepository
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    _last_touch: dict[str, float] = field(default_factory=dict[str, float])

    async def mint(
        self,
        *,
        org_id: str,
        label: str,
        role_name: str,
        created_by: str,
    ) -> MintedServiceToken:
        """Create a token; the raw value exists only in the return.

        Raises ``DuplicateServiceTokenLabelError`` when (org, label)
        is taken — label is the token's audit identity in the org.
        """
        raw_token = generate_service_token()
        record = ServiceTokenRecord(
            token_hash=hash_service_token(raw_token),
            org_id=org_id,
            label=label,
            role_name=role_name,
            created_by=created_by,
            created_at=self.now(),
            last_used_at=None,
        )
        await self.repo.create(record)
        logger.info(
            "service_token.minted",
            org_id=org_id,
            label=label,
            role_name=role_name,
            created_by=created_by,
        )
        return MintedServiceToken(raw_token=raw_token, record=record)

    async def list_for_org(self, org_id: str) -> list[ServiceTokenRecord]:
        return await self.repo.list_for_org(org_id)

    async def count_by_role(self, org_id: str) -> dict[str, int]:
        """Number of tokens per role name in this org.

        Used by the roles surface: the count shows next to the
        user-count label, and a role referenced by any token must not
        be deletable (the token would silently fail closed — correct
        but confusing when done by accident).
        """
        counts: dict[str, int] = {}
        for record in await self.repo.list_for_org(org_id):
            counts[record.role_name] = counts.get(record.role_name, 0) + 1
        return counts

    async def revoke(self, org_id: str, label: str) -> bool:
        revoked = await self.repo.delete_by_label(org_id, label)
        if revoked:
            logger.info(
                "service_token.revoked", org_id=org_id, label=label,
            )
        return revoked

    async def verify(self, raw_token: str) -> ServiceTokenRecord | None:
        """Resolve a presented bearer to its registry record.

        Returns None for anything that isn't a live service token —
        wrong prefix (without touching the repo), unknown hash,
        revoked. Revocation therefore bites on the next request.
        """
        if not raw_token.startswith(SERVICE_TOKEN_PREFIX):
            return None
        token_hash = hash_service_token(raw_token)
        record = await self.repo.get_by_hash(token_hash)
        if record is None:
            return None
        await self._maybe_touch(token_hash)
        return record

    async def _maybe_touch(self, token_hash: str) -> None:
        now_mono = self.monotonic()
        last = self._last_touch.get(token_hash)
        if (
            last is not None
            and now_mono - last < LAST_USED_WRITE_INTERVAL_SECONDS
        ):
            return
        self._last_touch[token_hash] = now_mono
        await self.repo.touch_last_used(token_hash, self.now())
