"""Shared test factories — call make_XXX() explicitly in each test."""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Collection, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import anyio
import mcp.types as mcp_types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyHttpUrl, AnyUrl

from mcpolis.adapters.repositories.audit_repository import AuditRepository
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.repositories.file_service_token_repository import (
    FileServiceTokenRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.audit import AuditEntry
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.service_token import (
    ServiceTokenRecord,
    hash_service_token,
)
from mcpolis.domain.model.settings import (
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import (
    DiscoveredTool,
    HttpTransportConfig,
    StdioTransportConfig,
    ToolAnnotations,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.email_sender import EmailSender
from mcpolis.domain.services.org_runtime import OrgRuntime, OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.domain.services.upstream_config_service import UpstreamConfigService
from mcpolis.domain.services.upstream_connection_service import (  # pyright: ignore[reportPrivateUsage]
    RefreshFailureSignature,
)
from mcpolis.domain.services.upstream_health_check import SignInWarner


def make_accepted_members(
    data_dir: Path,
    members: dict[str, str],
    *,
    org_id: str = DEFAULT_ORG_ID,
) -> None:
    """Record that each address in ``members`` (email → role) accepted its
    invitation to ``org_id``: the membership rows a file-backed
    (standalone) app reads from ``<data_dir>/memberships.json``.

    An address in the org's ``config.users`` without a row is only a
    pending invitation, with no access. Apps built from a config file
    seed their members here, before ``create_app``. Rows already in the
    file are kept."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "memberships.json"
    rows: list[dict[str, str]] = (
        json.loads(path.read_text()) if path.exists() else []
    )
    now = datetime.now(UTC).isoformat()
    rows.extend(
        {"org_id": org_id, "email": email, "role": role, "created_at": now}
        for email, role in members.items()
    )
    path.write_text(json.dumps(rows))


def make_config_users_accepted(data_dir: Path, config_json: str) -> None:
    """``make_accepted_members`` for every user of a standalone config
    file's text: the app treats them all as members, as if each had
    accepted their invitation."""
    users: dict[str, dict[str, str]] = json.loads(config_json).get("users", {})
    make_accepted_members(
        data_dir, {email: user["role"] for email, user in users.items()},
    )


def make_upstream_auth(
    mode: AuthMode = AuthMode.service_account,
) -> UpstreamAuthConfig:
    return UpstreamAuthConfig(mode=mode)


def make_upstream_definition(
    id: str = "test-upstream",
    display_name: str = "Test Upstream",
    transport: TransportType | None = None,
    command: str = "echo",
    url: str = "http://localhost:9999/mcp",
    auth: UpstreamAuthConfig | None = None,
    **kwargs: Any,
) -> UpstreamDefinition:
    if auth is None:
        auth = make_upstream_auth()
    # ``transport`` defaults to stdio for the historic
    # service_account case (most upstream tests just want a generic
    # upstream and don't care about transport). When the caller
    # passes an OAuth ``auth`` without naming a transport, default to
    # ``streamable_http`` instead — stdio + OAuth is a non-functional
    # shape rejected by the model validator (see
    # ``test_stdio_auth_mode_invariant.py``).
    if transport is None:
        transport = (
            TransportType.streamable_http
            if auth.mode != AuthMode.service_account
            else TransportType.stdio
        )
    if transport == TransportType.stdio:
        return UpstreamDefinition(
            id=id,
            display_name=display_name,
            transport=transport,
            stdio=StdioTransportConfig(command=command),
            auth=auth,
            **kwargs,
        )
    return UpstreamDefinition(
        id=id,
        display_name=display_name,
        transport=transport,
        http=HttpTransportConfig(url=url),
        auth=auth,
        **kwargs,
    )


def make_discovered_tool(
    upstream_id: str = "test-upstream",
    original_name: str = "do_thing",
    description: str = "Does a thing",
    input_schema: dict[str, Any] | None = None,
    annotations: ToolAnnotations | None = None,
) -> DiscoveredTool:
    return DiscoveredTool(
        upstream_id=upstream_id,
        original_name=original_name,
        prefixed_name=f"{upstream_id}__{original_name}",
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        annotations=annotations,
    )


def make_full_access_config(
    upstream_ids: list[str],
    user_emails: list[str],
    role_name: str = "default",
) -> SettingsConfig:
    """Config granting *user_emails* one role with access to every
    upstream in *upstream_ids*.

    PolicyEngine has no permissive fallback (zero roles = zero
    tools), so gateway-plumbing tests that don't exercise policy
    must seed a role like this rather than an empty config.
    """
    return SettingsConfig(
        roles={
            role_name: RoleDefinition(
                is_default=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(
                        mcps={uid: True for uid in upstream_ids},
                    ),
                ),
            ),
        },
        users={email: UserDefinition(role=role_name) for email in user_emails},
    )


def make_runtime_manager(
    policy_engine: PolicyEngine,
    tool_registry: ToolRegistry | None = None,
    client_manager: UpstreamClientManager | None = None,
    tool_router: ToolRouter | None = None,
    config_service: UpstreamConfigService | None = None,
    upstreams: list[UpstreamDefinition] | None = None,
    org_id: str = "default",
) -> OrgRuntimeManager:
    """Build an OrgRuntimeManager with a single pre-loaded runtime for tests."""
    from unittest.mock import MagicMock

    manager = OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )
    runtime = OrgRuntime(
        org_id=org_id,
        policy_engine=policy_engine,
        tool_registry=tool_registry or MagicMock(),
        client_manager=client_manager or MagicMock(),
        tool_router=tool_router or MagicMock(),
        config_service=config_service or MagicMock(),
        upstreams=upstreams or [],
    )
    manager._runtimes[org_id] = runtime
    manager._startup_status[org_id] = MagicMock(ready=True, total=0, connected=set(), failed=set())
    return manager


def make_oauth_upstream(
    id: str = "notion",
    display_name: str = "Notion",
    mode: AuthMode = AuthMode.admin_oauth,
    url: str = "https://mcp.example.invalid/mcp",
) -> UpstreamDefinition:
    """OAuth-mode upstream with a streamable_http transport.

    Consolidates the ``_make_upstream`` helper that was duplicated
    across ``test_upstream_oauth_silent_refresh.py``,
    ``test_refresh_failure_signature.py``,
    ``test_refresh_failure_policy.py``, ``test_liveness_probe.py``,
    and ``test_upstream_health_check.py``. Each copy defaulted to
    slightly different display_name / id combinations — keep a
    single shape here so any "standard OAuth upstream" test doesn't
    have to re-decide.
    """
    return UpstreamDefinition(
        id=id,
        display_name=display_name,
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=url),
        auth=UpstreamAuthConfig(mode=mode),
    )


def make_stored_oauth_token(
    access_token: str = "stored-at",
    refresh_token: str | None = "stored-rt",
    *,
    expires_in_minutes: float = 30.0,
) -> OAuthToken:
    """An ``OAuthToken`` with a configurable expiry offset.

    Positive ``expires_in_minutes`` → valid for that many minutes
    from now; negative → already expired. The OAuth-durability
    tests use both signs depending on whether they're exercising
    the silent-refresh or already-expired branch."""
    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        scopes=[],
        refresh_token_created_at=datetime.now(UTC),
    )


async def seed_oauth_storage(
    store: FileConnectionStore,
    *,
    org_id: str = DEFAULT_ORG_ID,
    upstream_id: str = "notion",
    user_id: str = "__admin__",
    callback_url: str = "https://gateway.example.invalid/api/oauth/upstream/callback",
    expires_in_minutes: float = 30.0,
    refresh_token: str | None = "stored-rt",
) -> OAuthToken:
    """Drop a user token + client_info into a connection store so a
    real ``_build_oauth_provider`` has everything it needs.

    Returns the seeded token so tests can assert identity
    post-reconnect. The client_info's ``redirect_uris`` must include
    ``callback_url`` — otherwise ``_build_oauth_provider``'s
    DCR self-heal path drops client_info and the SDK's
    ``can_refresh_token`` silently returns False, bypassing the
    refresh branch we want to pin in tests.
    """
    token = make_stored_oauth_token(
        refresh_token=refresh_token, expires_in_minutes=expires_in_minutes,
    )
    await store.put_user_token(org_id, user_id, upstream_id, token)
    await store.put_client_info(
        org_id, upstream_id, user_id,
        OAuthClientInformationFull(
            client_id="cid",
            client_secret="csec",
            redirect_uris=[AnyUrl(callback_url)],
            token_endpoint_auth_method="client_secret_post",
        ).model_dump(mode="json"),
    )
    return token


async def seed_refresh_failure_streak(
    store: FileConnectionStore,
    *,
    upstream_id: str,
    user_id: str,
    failures: int,
    started_ago: timedelta,
) -> None:
    """``failures`` refresh failures in a row of ``user_id``'s sign-in
    to ``upstream_id``, the first ``started_ago``: what a long streak
    leaves in the store, without living through it."""
    key = FileConnectionStore._failures_key(upstream_id, user_id)  # pyright: ignore[reportPrivateUsage]
    async with store._lock:  # pyright: ignore[reportPrivateUsage]
        data = store._read()  # pyright: ignore[reportPrivateUsage]
        data[key] = {
            "count": failures,
            "first_failure_at": (datetime.now(UTC) - started_ago).isoformat(),
        }
        store._write(data)  # pyright: ignore[reportPrivateUsage]


async def seed_sign_in_age(
    store: FileConnectionStore,
    *,
    upstream_id: str,
    user_id: str,
    age: timedelta,
) -> None:
    """Make ``user_id``'s stored sign-in to ``upstream_id`` last saved
    ``age`` ago: the periodic refresh renews a sign-in past its maximum
    age whatever its declared expiry."""
    key = FileConnectionStore._user_key(user_id, upstream_id)  # pyright: ignore[reportPrivateUsage]
    async with store._lock:  # pyright: ignore[reportPrivateUsage]
        data = store._read()  # pyright: ignore[reportPrivateUsage]
        data[key]["updated_at"] = (datetime.now(UTC) - age).isoformat()
        store._write(data)  # pyright: ignore[reportPrivateUsage]


def make_oauth_metadata(
    issuer: str = "https://oauth.example.invalid",
    authorization_endpoint: str | None = None,
    token_endpoint: str | None = None,
    registration_endpoint: str | None = None,
) -> OAuthMetadata:
    """Build an RFC 8414 ``OAuthMetadata`` whose ``token_endpoint`` is
    deliberately on a different host from the upstream MCP base URL.

    The §3.8 / §5.4 bug is specifically about Mixpanel-style upstreams
    whose token endpoint isn't ``<base>/token``: the SDK's refresh
    branch falls back to that path and 404s. Defaulting the endpoints
    to ``oauth.example.invalid`` (separate from the upstream's
    ``mcp.example.invalid``) keeps tests honest about that geometry —
    a fix that "works" only when token_endpoint == <base>/token would
    silently pass an asymmetric test.
    """
    base = issuer.rstrip("/")
    return OAuthMetadata(
        issuer=AnyHttpUrl(base),
        authorization_endpoint=AnyHttpUrl(
            authorization_endpoint or f"{base}/authorize",
        ),
        token_endpoint=AnyHttpUrl(token_endpoint or f"{base}/oauth/token"),
        registration_endpoint=(
            AnyHttpUrl(registration_endpoint)
            if registration_endpoint is not None
            else None
        ),
    )


def make_refresh_failure_signature(
    error_code: str | None = "invalid_grant",
    *,
    status_code: int | None = None,
    body_excerpt: str | None = None,
    timestamp: datetime | None = None,
) -> RefreshFailureSignature:
    """Build a ``RefreshFailureSignature`` with sensible defaults for
    each branch of the §5.1 delete-vs-retry policy.

    Defaults land on ``invalid_grant`` + status 400 (the
    notify/delete case). Pass ``error_code=None`` for the
    transient-5xx case — status falls through to 500.
    """
    if status_code is None:
        status_code = 400 if error_code == "invalid_grant" else 500
    if body_excerpt is None:
        body_excerpt = (
            '{"error":"invalid_grant"}' if error_code == "invalid_grant"
            else "<html>bad gateway</html>"
        )
    return RefreshFailureSignature(
        status_code=status_code,
        body_excerpt=body_excerpt,
        error_code=error_code,
        timestamp=timestamp or datetime.now(UTC),
    )


def make_service_token_record(
    label: str = "test-bot",
    org_id: str = DEFAULT_ORG_ID,
    role_name: str = "user",
    raw_token: str = "svct_test-raw-token",
    **kwargs: Any,
) -> ServiceTokenRecord:
    defaults: dict[str, Any] = {
        "token_hash": hash_service_token(raw_token),
        "org_id": org_id,
        "label": label,
        "role_name": role_name,
        "created_by": "admin@example.com",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_used_at": None,
    }
    defaults.update(kwargs)
    return ServiceTokenRecord(**defaults)


def make_audit_entry(
    user_id: str = "testuser",
    upstream_id: str = "test-upstream",
    tool: str = "test-upstream__do_thing",
    **kwargs: Any,
) -> AuditEntry:
    defaults: dict[str, Any] = {
        "timestamp": "2026-01-01T00:00:00Z",
        "user_id": user_id,
        "upstream_id": upstream_id,
        "auth_mode": "service_account",
        "auth_identity": f"service_account:{upstream_id}",
        "tool": tool,
        "policy_decision": "allowed",
        "response_status": "success",
        "latency_ms": 42.0,
        "session_id": None,
    }
    defaults.update(kwargs)
    return AuditEntry(**defaults)




@dataclass
class FakeOrgFacts:
    """``OrgFacts`` with fixed answers for every org."""
    admins: list[str] = field(default_factory=list)
    org_slug: str | None = "acme"
    org_name: str | None = "Acme"
    admin_signed_in: bool = False
    admin_lookup_error: Exception | None = None
    # Addresses that are not members of any org (everyone else is).
    non_members: set[str] = field(default_factory=set[str])
    # Upstream ids an admin stopped, in every org.
    stopped: set[str] = field(default_factory=set[str])

    async def admin_emails(self, org_id: str) -> list[str]:
        del org_id
        if self.admin_lookup_error is not None:
            raise self.admin_lookup_error
        return list(self.admins)

    def slug(self, org_id: str) -> str | None:
        del org_id
        return self.org_slug

    def display_name(self, org_id: str) -> str | None:
        del org_id
        return self.org_name

    async def has_admin_sign_in(
        self, org_id: str, upstream: UpstreamDefinition,
    ) -> bool:
        del org_id, upstream
        return self.admin_signed_in

    async def is_member(self, org_id: str, email: str) -> bool:
        del org_id
        return email not in self.non_members

    async def is_stopped(self, org_id: str, upstream_id: str) -> bool:
        del org_id
        return upstream_id in self.stopped


def make_sign_in_warner(
    email_sender: EmailSender,
    admins: list[str] | None = None,
    *,
    org_slug: str | None = "acme",
    org_name: str | None = "Acme",
    admin_signed_in: bool = False,
    admin_lookup_error: Exception | None = None,
    server_url: str = "https://gateway.example.invalid",
    non_members: set[str] | None = None,
    stopped: set[str] | None = None,
) -> SignInWarner:
    """A §5.2 warner whose emails land in ``email_sender``. Every org has
    ``admins`` as admins, slug ``org_slug``, name ``org_name``; an admin
    sign-in exists iff ``admin_signed_in``; everyone but ``non_members``
    is a member; the upstreams in ``stopped`` are stopped. Warnings after
    a deletion run in the background: ``await warner.drain()`` before
    asserting on them."""
    return SignInWarner(
        email_sender=email_sender,
        orgs=FakeOrgFacts(
            admins=list(admins or []),
            org_slug=org_slug,
            org_name=org_name,
            admin_signed_in=admin_signed_in,
            admin_lookup_error=admin_lookup_error,
            non_members=set(non_members or set()),
            stopped=set(stopped or set()),
        ),
        server_url=server_url,
    )


class RecordingEventBus:
    """An event stream that keeps what is published."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, org_id: str, event: Event) -> None:
        del org_id
        self.events.append(event)

    async def subscribe(
        self, org_id: str, user_email: str,
    ) -> AsyncIterator[Event | None]:
        del org_id, user_email
        for event in self.events:
            yield event

    async def close(self) -> None:
        return None


class RenameFailsOnceTokenRepository(FileServiceTokenRepository):
    """Token registry whose first role rename fails, like a store timeout."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.failures_left = 1

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> int:
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("token store unavailable")
        return await super().rename_role(org_id, old_name, new_name)


class Gate:
    """Holds every call that reaches it until the test opens it."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        if not self.release.is_set():
            self.reached.set()
            await self.release.wait()


class YieldingAuditRepository(AuditRepository):
    """Audit store that keeps its rows in memory, and whose write waits
    on the event loop first, like Mongo's insert. With a ``gate``, each
    write is also held there until the test opens it.

    ``FileAuditRepository`` never waits, so a cancellation that lands at
    an ``await`` inside the write can't be seen with it."""

    def __init__(self, gate: Gate | None = None) -> None:
        self.rows: list[AuditEntry] = []
        self.gate = gate

    async def log(self, org_id: str, entry: AuditEntry) -> None:
        del org_id
        await asyncio.sleep(0.01)  # the database round trip
        if self.gate is not None:
            await self.gate.hold()
        self.rows.append(entry)


class GatedTokenRepository(FileServiceTokenRepository):
    """Token registry whose ``gated`` call waits at ``gate`` before it
    touches the registry. Lets a test start a second admin action while
    the first one is half done."""

    def __init__(
        self, data_dir: Path, gated: Literal["create", "rename_role"],
    ) -> None:
        super().__init__(data_dir)
        self.gated = gated
        self.gate = Gate()

    async def create(self, record: ServiceTokenRecord) -> None:
        if self.gated == "create":
            await self.gate.hold()
        await super().create(record)

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> int:
        if self.gated == "rename_role":
            await self.gate.hold()
        return await super().rename_role(org_id, old_name, new_name)


class GatedConfigStore(FileConfigStore):
    """Config store whose ``gated`` call waits at ``gate``: before it
    touches the store, or with ``after=True`` once its write is done,
    like a slow database that has already applied the write."""

    def __init__(
        self,
        config_path: Path,
        gated: Literal[
            "rename_role", "delete_role", "create_role",
            "set_user", "remove_user", "set_user_role",
        ],
        *,
        after: bool = False,
    ) -> None:
        super().__init__(config_path)
        self.gated = gated
        self.after = after
        self.gate = Gate()

    async def _hold_if(self, call: str, after: bool) -> None:
        if call == self.gated and after == self.after:
            await self.gate.hold()

    async def rename_role(
        self, org_id: str, old_name: str, new_name: str
    ) -> SettingsConfig:
        await self._hold_if("rename_role", after=False)
        config = await super().rename_role(org_id, old_name, new_name)
        await self._hold_if("rename_role", after=True)
        return config

    async def delete_role(self, org_id: str, name: str) -> SettingsConfig:
        await self._hold_if("delete_role", after=False)
        config = await super().delete_role(org_id, name)
        await self._hold_if("delete_role", after=True)
        return config

    async def set_user(
        self, org_id: str, email: str, user: UserDefinition
    ) -> SettingsConfig:
        await self._hold_if("set_user", after=False)
        config = await super().set_user(org_id, email, user)
        await self._hold_if("set_user", after=True)
        return config

    async def remove_user(
        self,
        org_id: str,
        email: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        await self._hold_if("remove_user", after=False)
        config = await super().remove_user(org_id, email, eligible=eligible)
        await self._hold_if("remove_user", after=True)
        return config

    async def set_user_role(
        self,
        org_id: str,
        email: str,
        role: str,
        *,
        eligible: Collection[str] | None = None,
    ) -> SettingsConfig:
        await self._hold_if("set_user_role", after=False)
        config = await super().set_user_role(
            org_id, email, role, eligible=eligible,
        )
        await self._hold_if("set_user_role", after=True)
        return config

    async def create_role(
        self, org_id: str, name: str, copy_from: str | None = None,
    ) -> SettingsConfig:
        await self._hold_if("create_role", after=False)
        config = await super().create_role(org_id, name, copy_from=copy_from)
        await self._hold_if("create_role", after=True)
        return config


class GatedConnectionStore(FileConnectionStore):
    """Connection store whose ``gated`` call waits at ``gate``: before it
    touches the store, or with ``after=True`` once its write is done."""

    def __init__(
        self,
        data_dir: Path,
        gated: Literal[
            "set_disabled", "delete_all_for_upstream",
            "clear_connection_error", "get_user_token", "delete_user_token",
        ],
        *,
        after: bool = False,
    ) -> None:
        super().__init__(data_dir)
        self.gated = gated
        self.after = after
        self.gate = Gate()

    async def _hold_if(self, call: str, after: bool) -> None:
        if call == self.gated and after == self.after:
            await self.gate.hold()

    async def set_disabled(self, org_id: str, upstream_id: str) -> None:
        await self._hold_if("set_disabled", after=False)
        await super().set_disabled(org_id, upstream_id)
        await self._hold_if("set_disabled", after=True)

    async def clear_connection_error(self, org_id: str, upstream_id: str) -> None:
        await self._hold_if("clear_connection_error", after=False)
        await super().clear_connection_error(org_id, upstream_id)
        await self._hold_if("clear_connection_error", after=True)

    async def get_user_token(
        self, org_id: str, user_id: str, upstream_id: str,
    ) -> OAuthToken | None:
        await self._hold_if("get_user_token", after=False)
        token = await super().get_user_token(org_id, user_id, upstream_id)
        await self._hold_if("get_user_token", after=True)
        return token

    async def delete_user_token(
        self, org_id: str, user_id: str, upstream_id: str,
    ) -> None:
        await self._hold_if("delete_user_token", after=False)
        await super().delete_user_token(org_id, user_id, upstream_id)
        await self._hold_if("delete_user_token", after=True)

    async def delete_all_for_upstream(
        self, org_id: str, upstream_id: str,
    ) -> int:
        await self._hold_if("delete_all_for_upstream", after=False)
        deleted = await super().delete_all_for_upstream(org_id, upstream_id)
        await self._hold_if("delete_all_for_upstream", after=True)
        return deleted


async def run_while_gated[A, B](
    gate: Gate,
    first: Callable[[], Coroutine[object, object, A]],
    second: Callable[[], Coroutine[object, object, B]],
) -> tuple[A, B, bool]:
    """Start *first*, wait until it is held at *gate*, start *second*,
    then open the gate.

    Returns both results, and whether *second* finished while *first*
    was still held: True means the two actions overlapped.
    """
    first_task = asyncio.create_task(first())
    await asyncio.wait_for(gate.reached.wait(), timeout=5)
    second_task = asyncio.create_task(second())
    finished, _ = await asyncio.wait({second_task}, timeout=0.2)
    gate.release.set()
    return await first_task, await second_task, bool(finished)


async def cancel_while_gated(
    gate: Gate, call: Callable[[], Coroutine[object, object, object]],
) -> None:
    """Run *call* in its own anyio cancel scope and cancel that scope
    while *call* is held at *gate*: what Starlette's ``BaseHTTPMiddleware``
    does to a dashboard request, and what the MCP SDK does to a tool
    call's handler.

    It stops there: for an MCP tool call, ``cancel_mcp_call_while_gated``
    also runs the answer the SDK sends once the handler returns, which
    this skips."""
    scope = anyio.CancelScope()

    async def run() -> None:
        with scope:
            await call()

    task = asyncio.create_task(run())
    await asyncio.wait_for(gate.reached.wait(), timeout=5)
    scope.cancel()
    # Let the cancellation land before the held call may go on.
    await asyncio.sleep(0.05)
    gate.release.set()
    await task


async def cancel_natively_while_gated(
    gate: Gate, call: Callable[[], Coroutine[object, object, object]],
) -> None:
    """Run *call* in its own task and ``Task.cancel()`` it while it is
    held at *gate*: what uvicorn does to the request tasks still running
    when its graceful-shutdown time is up. An anyio shield does not stop
    this kind of cancel. Raises what *call* raised, other than the
    cancel."""
    task = asyncio.create_task(call())
    await asyncio.wait_for(gate.reached.wait(), timeout=5)
    task.cancel()
    # Let the cancellation land before the held call may go on.
    await asyncio.sleep(0.05)
    gate.release.set()
    await asyncio.wait({task}, timeout=5)
    if task.done() and not task.cancelled():
        failure = task.exception()
        if failure is not None:
            raise failure


# Request 0 of an in-memory MCP session is ``initialize``; the first
# call after it is request 1.
FIRST_CALL_ID = 1


def make_cancel_notification(request_id: int) -> mcp_types.ClientNotification:
    """What an MCP client sends when its user gives up on a request."""
    return mcp_types.ClientNotification(mcp_types.CancelledNotification(
        method="notifications/cancelled",
        params=mcp_types.CancelledNotificationParams(
            requestId=request_id, reason="user pressed Esc",
        ),
    ))


def make_bearer_auth(caller: str) -> AuthenticatedUser:
    """The identity a bearer token gives an MCP request."""
    return AuthenticatedUser(
        AccessToken(token="test-token", client_id=caller, scopes=[]),
    )


async def cancel_mcp_call_while_gated(
    server: FastMCP,
    gate: Gate,
    tool: str,
    arguments: dict[str, Any],
    *,
    caller: str,
) -> None:
    """Call *tool* as *caller* over a real (in-memory) MCP session, and
    cancel the call the way an MCP client does (``notifications/cancelled``)
    while it is held at *gate*. Then open the gate, check the same session
    still answers, and close it.

    Returns once the server has ended every handler, so the caller can
    read the end state. Unlike ``cancel_while_gated`` this runs the SDK's
    whole request handling, including the answer it sends once a handler
    returns: a handler that returns normally after the client's cancel
    makes the SDK answer the request twice, which raises "Request already
    responded to" and kills the session. That surfaces here as an
    exception.
    """
    auth = auth_context_var.set(make_bearer_auth(caller))
    try:
        async with asyncio.timeout(10):
            async with create_connected_server_and_client_session(
                server,
            ) as client:
                call = asyncio.create_task(client.call_tool(tool, arguments))
                await gate.reached.wait()
                await client.send_notification(
                    make_cancel_notification(FIRST_CALL_ID),
                )
                with contextlib.suppress(McpError):  # "Request cancelled"
                    await call
                gate.release.set()
                # Let the held call end, then prove the session serves on.
                await asyncio.sleep(0.1)
                await client.send_ping()
            # Leaving the session waits for the server's handlers to end.
    finally:
        auth_context_var.reset(auth)
