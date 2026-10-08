"""The stored form of each gateway OAuth state item.

Shared by the file repository (one JSON file) and the Mongo repository
(one encrypted document per item), so both write the same JSON for a
client, a token or an approval, and read back what the other wrote.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import structlog
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import TypeAdapter

from mcpolis.domain.ports.oauth_state_repository import (
    StoredAccessToken,
    StoredClient,
    StoredClientApproval,
    StoredRefreshToken,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# A JSON object as ``json.loads`` returns it.
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ItemCodec[T]:
    """How one kind of item is written and read back."""

    # The kind's name: the ``OAuthStateSnapshot`` / ``OAuthStateChanges``
    # field, the section of the JSON file, and the Mongo ``kind``.
    kind: str
    adapter: TypeAdapter[T]
    # The key the item is stored under (see ``OAuthStateChanges``).
    key_of: Callable[[T], str]
    # Reads JSON this codec cannot (an older layout), or ``None``.
    read_older: Callable[[JsonObject], T | None] | None = None

    def encode(self, item: T) -> JsonObject:
        return cast(JsonObject, self.adapter.dump_python(item, mode="json"))

    def decode(self, data: JsonObject) -> T:
        if self.read_older is not None:
            older = self.read_older(data)
            if older is not None:
                return older
        return self.adapter.validate_python(data)

    def decode_all(self, raw: object) -> dict[str, T]:
        """Read a ``{key: item}`` JSON object, skipping (and logging)
        any item that does not parse."""
        items: dict[str, T] = {}
        if not isinstance(raw, dict):
            return items
        for key, data in cast(dict[object, object], raw).items():
            item = self.decode_or_none(data)
            if isinstance(key, str) and item is not None:
                items[key] = item
        return items

    def decode_or_none(self, data: object) -> T | None:
        try:
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            return self.decode(cast(JsonObject, data))
        except (ValueError, TypeError):  # pydantic's ValidationError included
            logger.warning("gateway_oauth.stored_item.invalid", kind=self.kind)
            return None


def _read_bare_registration(data: JsonObject) -> StoredClient | None:
    """Clients used to be stored as the bare registration, with no
    ``info`` wrapper and no expiry fields."""
    if "info" in data:
        return None
    return StoredClient.from_before_expiry(
        OAuthClientInformationFull.model_validate(data),
    )


def _client_id_of(client: StoredClient) -> str:
    client_id = client.info.client_id
    if client_id is None:
        raise ValueError("a stored client has no client_id")
    return client_id


CLIENTS = ItemCodec[StoredClient](
    kind="clients",
    adapter=TypeAdapter(StoredClient),
    key_of=_client_id_of,
    read_older=_read_bare_registration,
)
ACCESS_TOKENS = ItemCodec[StoredAccessToken](
    kind="access_tokens",
    adapter=TypeAdapter(StoredAccessToken),
    key_of=lambda token: token.token,
)
REFRESH_TOKENS = ItemCodec[StoredRefreshToken](
    kind="refresh_tokens",
    adapter=TypeAdapter(StoredRefreshToken),
    key_of=lambda token: token.token,
)
CLIENT_APPROVALS = ItemCodec[StoredClientApproval](
    kind="client_approvals",
    adapter=TypeAdapter(StoredClientApproval),
    key_of=lambda approval: approval.key,
)
