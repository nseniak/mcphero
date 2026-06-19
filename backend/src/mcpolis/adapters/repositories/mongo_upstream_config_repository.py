"""Mongo-backed ``UpstreamConfigRepository`` — merged upstream view.

Mirrors ``FileUpstreamConfigStore`` but reads from the single
``upstreams`` collection where each document carries both the MCP
connection block and the MCPolis options.

Encryption at rest: ``server_config`` and ``options`` carry user-
supplied credential material (env vars, custom headers, OAuth
client_secret, etc.). They are JSON-serialized and stored under
``server_config_encrypted`` / ``options_encrypted`` — the
``OrgScopedCollection`` wrapper transparently encrypts those string
fields via the ``ENCRYPTED_FIELDS`` allowlist. Never write the legacy
plaintext ``server_config`` / ``options`` keys; the migration script
``upstreams_encrypt_phase_a`` strips them from existing rows.
"""
from __future__ import annotations

import json
from typing import Any

from mcpolis.adapters.repositories.mongo_client import OrgScopedCollection
from mcpolis.adapters.repositories.upstream_config_loader import (
    build_upstream,
    server_config_from_upstream,
)
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.domain.model.settings import OAuthAppsConfig, UpstreamOptions
from mcpolis.domain.model.upstream import UpstreamDefinition
from mcpolis.domain.ports.upstream_config_repository import UpstreamConfigRepository


class MongoUpstreamConfigRepository(UpstreamConfigStore, UpstreamConfigRepository):
    def __init__(
        self,
        upstreams: OrgScopedCollection,
        _config: OrgScopedCollection,
        oauth_apps: OAuthAppsConfig | None = None,
        allow_stdio: bool = True,
    ) -> None:
        self._upstreams = upstreams
        # ``_config`` is reserved for future per-org defaults; today the
        # merged view needs nothing from it because options live on the
        # upstream doc itself.
        self._oauth_apps = oauth_apps or {}
        self._allow_stdio = allow_stdio

    @staticmethod
    def _decode_blob(doc: dict[str, Any], encrypted_key: str) -> dict[str, Any]:
        """Decode a JSON-string blob written under ``<key>_encrypted``.

        ``OrgScopedCollection`` has already decrypted the value to the
        plaintext JSON string by the time we see it here; this helper
        just parses it back into a dict.
        """
        raw: Any = doc.get(encrypted_key)
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in parsed.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(k, str):
                out[k] = v
        return out

    async def _load_one(
        self, doc: dict[str, Any]
    ) -> UpstreamDefinition | None:
        upstream_id: Any = doc.get("upstream_id")
        if not isinstance(upstream_id, str):
            return None
        server_config = self._decode_blob(doc, "server_config_encrypted")
        options_dict = self._decode_blob(doc, "options_encrypted")
        try:
            return build_upstream(
                upstream_id,
                server_config,
                options_dict,
                oauth_apps=self._oauth_apps,
                allow_stdio=self._allow_stdio,
            )
        except ValueError:
            return None

    async def get_all(self, org_id: str) -> list[UpstreamDefinition]:
        docs = await self._upstreams.find_many(org_id)
        result: list[UpstreamDefinition] = []
        for doc in docs:
            upstream = await self._load_one(doc)
            if upstream is not None:
                result.append(upstream)
        return result

    async def get(
        self, org_id: str, upstream_id: str
    ) -> UpstreamDefinition | None:
        doc = await self._upstreams.find_one(
            org_id, {"upstream_id": upstream_id}
        )
        if doc is None:
            return None
        return await self._load_one(doc)

    @staticmethod
    def _encode_blob(payload: dict[str, Any]) -> str:
        # Stable key order keeps ciphertext-for-the-same-input
        # deterministic across repo restarts (modulo nonce), which
        # makes diffing test fixtures and migration plans easier.
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _options_payload(upstream: UpstreamDefinition) -> dict[str, Any]:
        options = UpstreamOptions(
            display_name=upstream.display_name,
            auth_mode=upstream.auth.mode.value,
            client_id=upstream.auth.client_id,
            client_secret=upstream.auth.client_secret,
            scopes=upstream.auth.scopes,
            default_arguments=upstream.default_arguments,
        )
        return options.model_dump(mode="json")

    async def add(self, org_id: str, upstream: UpstreamDefinition) -> None:
        server_config = server_config_from_upstream(upstream)
        await self._upstreams.insert_one(
            org_id,
            {
                "upstream_id": upstream.id,
                "server_config_encrypted": self._encode_blob(server_config),
                "options_encrypted": self._encode_blob(
                    self._options_payload(upstream)
                ),
            },
        )

    async def update(self, org_id: str, upstream: UpstreamDefinition) -> None:
        existing = await self._upstreams.find_one(
            org_id, {"upstream_id": upstream.id}
        )
        if existing is None:
            raise ValueError(f"Upstream '{upstream.id}' not found")
        # Always re-serialize ``server_config`` from the in-memory
        # upstream so a resource-only PUT (sandbox resources) is
        # persisted alongside the options write. Without this, resource
        # changes only live in the client_manager cache and reset on
        # the next reload.
        await self._upstreams.replace_one(
            org_id,
            {"upstream_id": upstream.id},
            {
                "upstream_id": upstream.id,
                "server_config_encrypted": self._encode_blob(
                    server_config_from_upstream(upstream)
                ),
                "options_encrypted": self._encode_blob(
                    self._options_payload(upstream)
                ),
            },
            upsert=True,
        )

    async def update_server_config(
        self, org_id: str, upstream_id: str, server_config: dict[str, Any]
    ) -> None:
        existing = await self._upstreams.find_one(
            org_id, {"upstream_id": upstream_id}
        )
        if existing is None:
            raise ValueError(f"Upstream '{upstream_id}' not found")
        # ``find_one`` already decrypted ``options_encrypted`` to its
        # plaintext JSON string; preserve it untouched on the way back.
        existing_options_blob: Any = existing.get("options_encrypted")
        if not isinstance(existing_options_blob, str):
            existing_options_blob = self._encode_blob({})
        await self._upstreams.replace_one(
            org_id,
            {"upstream_id": upstream_id},
            {
                "upstream_id": upstream_id,
                "server_config_encrypted": self._encode_blob(server_config),
                "options_encrypted": existing_options_blob,
            },
            upsert=True,
        )

    async def remove(self, org_id: str, upstream_id: str) -> None:
        await self._upstreams.delete_one(
            org_id, {"upstream_id": upstream_id}
        )

    async def delete_all_for_org(self, org_id: str) -> None:
        # Org-deletion cascade. ``OrgScopedCollection`` scopes the empty
        # filter to this org, so no other tenant's upstreams are touched.
        await self._upstreams.delete_many(org_id, {})
