"""A service-account token survives save + reload, and so does every
other upstream setting.

The Admin MCP ``add_upstream`` tool and the dashboard's
``POST /api/admin/upstreams`` both take an ``auth_token``. It is saved
as the secret Variable ``MCP_AUTH_TOKEN``, and the transport config the
stores persist refers to it: the ``Authorization: Bearer
${MCP_AUTH_TOKEN}`` header for HTTP, the ``MCP_AUTH_TOKEN`` env var for
stdio. It used to sit on a separate field no store saved, so it was
gone on the next read, including the dashboard's first Start.
"""
from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from pydantic import BaseModel

from mcpolis.adapters.repositories.encryption import FieldEncryptor
from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.repositories.file_upstream_config_store import (
    FileUpstreamConfigStore,
)
from mcpolis.adapters.repositories.mcp_json_store import McpJsonStore
from mcpolis.adapters.repositories.mongo_client import (
    COLL_CONFIG,
    COLL_UPSTREAMS,
    OrgScopedCollection,
)
from mcpolis.adapters.repositories.mongo_upstream_config_repository import (
    MongoUpstreamConfigRepository,
)
from mcpolis.adapters.repositories.upstream_config_loader import (
    server_config_from_upstream,
)
from mcpolis.adapters.repositories.upstream_config_store import UpstreamConfigStore
from mcpolis.adapters.upstream_clients.client_manager import UpstreamClientManager
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.subscription import PlanName
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    StdioTransportConfig,
    TransportType,
    UpstreamDefinition,
    has_service_account_token,
    with_service_account_token,
)
from mcpolis.domain.model.template_var import TemplateVarSummary
from mcpolis.domain.ports import DEFAULT_ORG_ID
from tests.unit.mongo_fixture import mongo_available, temp_mongo_database
from tests.unit.test_admin_mcp import (
    _build_admin_server,  # pyright: ignore[reportPrivateUsage]
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_users_only_admin,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
)
from tests.unit.test_dashboard_api import make_test_client

BACKENDS: list[str] = ["file"] + (["mongo"] if mongo_available() else [])

# Never contacted. Loopback passes the URL safety check under the unit
# runner (MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK); a made-up public host
# would fail its DNS lookup.
UPSTREAM_URL = "http://127.0.0.1:9/mcp"


def make_file_store_over(tmp_path: Path) -> FileUpstreamConfigStore:
    return FileUpstreamConfigStore(
        McpJsonStore(tmp_path / "mcp.json"),
        FileConfigStore(tmp_path / "config.json"),
    )


HTTP_REFERENCE = {"Authorization": "Bearer ${MCP_AUTH_TOKEN}"}
STDIO_REFERENCE = {"MCP_AUTH_TOKEN": "${MCP_AUTH_TOKEN}"}


def make_variables_over(tmp_path: Path) -> FileTemplateVarRepository:
    """The Variable store both test doors write to."""
    return FileTemplateVarRepository(tmp_path / "data")


async def read_variables(
    tmp_path: Path, upstream_id: str,
) -> list[TemplateVarSummary]:
    return await make_variables_over(tmp_path).list_summaries(
        DEFAULT_ORG_ID, upstream_id,
    )


async def assert_token_saved_as_secret_variable(
    tmp_path: Path, upstream_id: str, token: str,
) -> None:
    variables = await read_variables(tmp_path, upstream_id)
    # A password's summary never carries its value (write-only); the
    # value is what substitution reads.
    assert [(v.name, v.value, v.is_secret) for v in variables] == [
        ("MCP_AUTH_TOKEN", None, True),
    ]
    assert await make_variables_over(tmp_path).get_value(
        DEFAULT_ORG_ID, upstream_id, "MCP_AUTH_TOKEN",
    ) == token


@asynccontextmanager
async def make_upstream_stores(
    backend: str, tmp_path: Path,
) -> AsyncIterator[Callable[[], UpstreamConfigStore]]:
    """Yield a factory of store instances over ONE storage. A second
    instance reads only what the first one persisted, as after a
    restart. The file store matches ``_build_admin_server``'s files."""
    if backend == "file":
        yield lambda: make_file_store_over(tmp_path)
        return
    async with temp_mongo_database() as db:
        encryptor = FieldEncryptor.from_master_secret("unit-test-key")

        def make_mongo_store() -> UpstreamConfigStore:
            return MongoUpstreamConfigRepository(
                OrgScopedCollection(
                    db[COLL_UPSTREAMS], COLL_UPSTREAMS, encryptor=encryptor,
                ),
                OrgScopedCollection(
                    db[COLL_CONFIG], COLL_CONFIG, encryptor=encryptor,
                ),
            )
        yield make_mongo_store


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_admin_mcp_add_upstream_token_survives_reload(
    backend: str, tmp_path: Path,
) -> None:
    async with make_upstream_stores(backend, tmp_path) as make_store:
        parts = await make_admin_parts(
            tmp_path,
            config=_config_users_only_admin(),
            plan=PlanName.team,
            upstream_store=None if backend == "file" else make_store(),
        )
        server = parts.server
        await _call(server, "add_upstream", {
            "mcp_id": "remote", "display_name": "Remote",
            "transport": "streamable_http",
            "url": UPSTREAM_URL,
            "auth_token": "tok-http-secret",
        })
        await _call(server, "add_upstream", {
            "mcp_id": "local", "display_name": "Local",
            "transport": "stdio", "command": "echo",
            "auth_token": "tok-stdio-secret",
        })

        # What the reporter saw: get_upstream reads the store. The
        # token itself is never shown to the AI client.
        for mcp_id, secret in (
            ("remote", "tok-http-secret"), ("local", "tok-stdio-secret"),
        ):
            shown = await _call(server, "get_upstream", {"mcp_id": mcp_id})
            assert mcp_id in shown and secret not in shown, shown

        # After a restart: a fresh store instance over the same storage.
        reloaded = {
            u.id: u for u in await make_store().get_all(DEFAULT_ORG_ID)
        }
        reloaded_http = reloaded["remote"].http
        assert reloaded_http is not None
        assert reloaded_http.headers == HTTP_REFERENCE
        reloaded_stdio = reloaded["local"].stdio
        assert reloaded_stdio is not None
        assert reloaded_stdio.env == STDIO_REFERENCE
    await assert_token_saved_as_secret_variable(
        tmp_path, "remote", "tok-http-secret",
    )
    await assert_token_saved_as_secret_variable(
        tmp_path, "local", "tok-stdio-secret",
    )


@pytest.mark.asyncio
async def test_dashboard_add_upstream_token_survives_reload(
    tmp_path: Path,
) -> None:
    client = make_test_client(tmp_path)
    remote = client.post("/api/admin/upstreams", json={
        "id": "remote", "display_name": "Remote",
        "url": UPSTREAM_URL,
        "auth_mode": "service_account", "auth_token": "tok-http-secret",
    })
    assert remote.status_code == 201, remote.text
    local = client.post("/api/admin/upstreams", json={
        "id": "local", "display_name": "Local", "command": "echo",
        "auth_mode": "service_account", "auth_token": "tok-stdio-secret",
    })
    assert local.status_code == 201, local.text

    reloaded = {
        u.id: u
        for u in await make_file_store_over(tmp_path).get_all(DEFAULT_ORG_ID)
    }
    reloaded_http = reloaded["remote"].http
    assert reloaded_http is not None
    assert reloaded_http.headers == HTTP_REFERENCE
    reloaded_stdio = reloaded["local"].stdio
    assert reloaded_stdio is not None
    assert reloaded_stdio.env == STDIO_REFERENCE
    await assert_token_saved_as_secret_variable(
        tmp_path, "remote", "tok-http-secret",
    )
    await assert_token_saved_as_secret_variable(
        tmp_path, "local", "tok-stdio-secret",
    )
    assert_saved_nowhere(tmp_path, "tok-http-secret")
    assert_saved_nowhere(tmp_path, "tok-stdio-secret")


@pytest.mark.asyncio
async def test_dashboard_refuses_a_token_and_a_variable_of_the_same_name(
    tmp_path: Path,
) -> None:
    """The token becomes the Variable MCP_AUTH_TOKEN; a Variable of that
    name sent alongside would be silently overwritten."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams", json={
        "id": "remote", "display_name": "Remote", "url": UPSTREAM_URL,
        "auth_mode": "service_account", "auth_token": "tok-secret",
        "template_vars": {"MCP_AUTH_TOKEN": {"value": "other"}},
    })
    assert resp.status_code == 400, resp.text
    assert "MCP_AUTH_TOKEN" in resp.text and "tok-secret" not in resp.text
    assert await make_file_store_over(tmp_path).get(
        DEFAULT_ORG_ID, "remote",
    ) is None


def assert_saved_nowhere(tmp_path: Path, secret: str) -> None:
    """Not in the saved MCP config (the Variable store is checked apart)."""
    for saved in ("mcp.json", "config.json"):
        assert secret not in (tmp_path / saved).read_text(), saved


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", [
    # After "Move to Variables" the dashboard sends the header as
    # ``Bearer ${NAME}`` AND the raw token it parsed earlier.
    {"url": UPSTREAM_URL, "headers": {"Authorization": "Bearer ${API_KEY}"}},
    # A pasted config often spells the header in lower case.
    {"url": UPSTREAM_URL, "headers": {"authorization": "Bearer ${API_KEY}"}},
    # The stdio twin: an env MCP_AUTH_TOKEN the caller set.
    {"command": "echo", "env": {"MCP_AUTH_TOKEN": "${TOKEN}"}},
])
async def test_dashboard_add_upstream_keeps_caller_token_over_raw_token(
    connection: dict[str, object], tmp_path: Path,
) -> None:
    """A header or env entry the caller set wins over ``auth_token``:
    it is saved as sent, and the raw token is saved nowhere."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams", json={
        "id": "mcp", "display_name": "MCP", "auth_mode": "service_account",
        "auth_token": "raw-secret-must-not-be-saved", **connection,
    })
    assert resp.status_code == 201, resp.text

    reloaded = await make_file_store_over(tmp_path).get(DEFAULT_ORG_ID, "mcp")
    assert reloaded is not None
    assert server_config_from_upstream(reloaded) == connection
    assert_saved_nowhere(tmp_path, "raw-secret-must-not-be-saved")
    assert await read_variables(tmp_path, "mcp") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "   "])
@pytest.mark.parametrize("connection", [
    {"url": UPSTREAM_URL}, {"command": "echo"},
])
async def test_dashboard_add_upstream_without_token_adds_no_auth(
    connection: dict[str, object], token: str | None, tmp_path: Path,
) -> None:
    """The URL tab sends no ``auth_token``. Nothing may be added: an
    empty ``Bearer `` header would fail every connect."""
    client = make_test_client(tmp_path)
    body: dict[str, object] = {
        "id": "mcp", "display_name": "MCP", "auth_mode": "service_account",
        **connection,
    }
    if token is not None:
        body["auth_token"] = token
    resp = client.post("/api/admin/upstreams", json=body)
    assert resp.status_code == 201, resp.text

    reloaded = await make_file_store_over(tmp_path).get(DEFAULT_ORG_ID, "mcp")
    assert reloaded is not None
    assert server_config_from_upstream(reloaded) == connection
    assert await read_variables(tmp_path, "mcp") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("connection", "token"), [
    ({"url": UPSTREAM_URL}, "tok\nsecret-tail"),
    ({"url": UPSTREAM_URL}, "tok\x00secret-tail"),
    ({"url": UPSTREAM_URL}, "t\u00f6k-secret-tail"),
    ({"command": "echo"}, "tok\nsecret-tail"),
])
async def test_dashboard_refuses_an_unsendable_token_without_echoing_it(
    connection: dict[str, object], token: str, tmp_path: Path,
) -> None:
    """A line break inside the token (or non-ASCII in a header) would
    fail every connect, and the HTTP client's error prints the header,
    token included, into the logs. Refused before anything is saved."""
    client = make_test_client(tmp_path)
    resp = client.post("/api/admin/upstreams", json={
        "id": "mcp", "display_name": "MCP", "auth_mode": "service_account",
        "auth_token": token, **connection,
    })
    assert resp.status_code == 400, resp.text
    assert "secret-tail" not in resp.text
    assert await make_file_store_over(tmp_path).get(DEFAULT_ORG_ID, "mcp") is None


@pytest.mark.asyncio
async def test_admin_mcp_add_upstream_says_when_it_cannot_save_the_token(
    tmp_path: Path,
) -> None:
    server, _ = await _build_admin_server(
        tmp_path, config=_config_users_only_admin(), plan=PlanName.team,
    )
    refused = await _call(server, "add_upstream", {
        "mcp_id": "remote", "display_name": "Remote",
        "transport": "streamable_http", "url": UPSTREAM_URL,
        "auth_token": "tok\nsecret-tail",
    })
    assert refused.startswith("Error: auth_token contains"), refused
    assert "secret-tail" not in refused
    assert "not found" in await _call(
        server, "get_upstream", {"mcp_id": "remote"},
    )

    ignored = await _call(server, "add_upstream", {
        "mcp_id": "remote", "display_name": "Remote",
        "transport": "streamable_http", "url": UPSTREAM_URL,
        "auth_mode": "per_user_oauth", "auth_token": "tok",
    })
    assert "added" in ignored and "auth_token was not saved" in ignored


@pytest.mark.parametrize(
    "mode", [AuthMode.per_user_oauth, AuthMode.admin_oauth],
)
def test_with_service_account_token_ignores_oauth_modes(
    mode: AuthMode,
) -> None:
    upstream = UpstreamDefinition(
        id="remote", display_name="Remote",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=UPSTREAM_URL),
        auth=UpstreamAuthConfig(mode=mode),
    )
    assert with_service_account_token(upstream, "tok") == (upstream, None)


@pytest.mark.parametrize(("mode", "headers", "expected"), [
    (AuthMode.service_account, {"Authorization": "Bearer ${MCP_AUTH_TOKEN}"}, True),
    (AuthMode.service_account, {"authorization": "Bearer x"}, True),
    (AuthMode.service_account, {"X-Api-Key": "x"}, False),
    (AuthMode.admin_oauth, {"Authorization": "Bearer x"}, False),
    (AuthMode.per_user_oauth, {"Authorization": "Bearer x"}, False),
])
def test_has_service_account_token_for_http(
    mode: AuthMode, headers: dict[str, str], expected: bool,
) -> None:
    """What ``get_upstream`` reports as ``has_token``."""
    upstream = UpstreamDefinition(
        id="remote", display_name="Remote",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=UPSTREAM_URL, headers=headers),
        auth=UpstreamAuthConfig(mode=mode),
    )
    assert has_service_account_token(upstream) is expected


@pytest.mark.parametrize(("env", "expected"), [
    ({"MCP_AUTH_TOKEN": "${MCP_AUTH_TOKEN}"}, True), ({"OTHER": "x"}, False),
])
def test_has_service_account_token_for_stdio(
    env: dict[str, str], expected: bool,
) -> None:
    upstream = UpstreamDefinition(
        id="local", display_name="Local", transport=TransportType.stdio,
        stdio=StdioTransportConfig(command="echo", env=env),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )
    assert has_service_account_token(upstream) is expected


def make_every_setting_upstreams() -> list[UpstreamDefinition]:
    """Between them, these set every saved field away from its default,
    so a field a store fails to save cannot pass the round trip."""
    return [
        UpstreamDefinition(
            id="remote", display_name="Remote",
            transport=TransportType.streamable_http,
            http=HttpTransportConfig(
                url=UPSTREAM_URL, headers={"X-Api-Key": "key"},
            ),
            auth=UpstreamAuthConfig(
                mode=AuthMode.admin_oauth, client_id="client",
                client_secret="secret", scopes=["read"],
            ),
            default_arguments={"search": {"limit": 5}},
        ),
        UpstreamDefinition(
            id="local", display_name="Local", transport=TransportType.stdio,
            stdio=StdioTransportConfig(
                command="npx", args=["-y", "some-mcp"],
                env={"MCP_AUTH_TOKEN": "tok"},
                cpu_vcpus=2.0, memory_mb=2048, disk_gb=5, pids_limit=64,
                tmpfs_mb=128, persistent_disk_enabled=True,
            ),
            auth=UpstreamAuthConfig(mode=AuthMode.service_account),
        ),
    ]


def make_bare_copy(upstream: UpstreamDefinition) -> UpstreamDefinition:
    """Same id and transport, every other setting at its default."""
    return UpstreamDefinition(
        id=upstream.id, display_name="", transport=upstream.transport,
        stdio=(
            StdioTransportConfig(command=upstream.stdio.command)
            if upstream.stdio is not None else None
        ),
        http=(
            HttpTransportConfig(url=upstream.http.url)
            if upstream.http is not None else None
        ),
        auth=UpstreamAuthConfig(mode=AuthMode.service_account),
    )


def fields_left_at_default(upstreams: list[UpstreamDefinition]) -> set[str]:
    """Saved fields that no upstream in *upstreams* sets."""
    models: list[BaseModel] = []
    for u in upstreams:
        models += [u, u.auth, *(m for m in (u.stdio, u.http) if m is not None)]
    left: set[str] = set()
    for cls in {type(m) for m in models}:
        for name, field in cls.model_fields.items():
            # A runtime-only field and a class constant are never saved.
            if field.exclude or name == "PREFIX_SEPARATOR":
                continue
            default = field.get_default(call_default_factory=True)
            if all(
                getattr(m, name) == default for m in models if type(m) is cls
            ):
                left.add(f"{cls.__name__}.{name}")
    return left


@pytest.mark.asyncio
@pytest.mark.parametrize("save_path", ["add", "update"])
@pytest.mark.parametrize("backend", BACKENDS)
async def test_every_upstream_setting_survives_a_store_round_trip(
    backend: str, save_path: str, tmp_path: Path,
) -> None:
    """Guards the bug class behind the lost token: a setting the app
    holds in memory that a store never saves. A new field fails the
    first assert until the fixtures set it, then the round trip until
    both stores save it."""
    upstreams = make_every_setting_upstreams()
    assert fields_left_at_default(upstreams) == set()
    async with make_upstream_stores(backend, tmp_path) as make_store:
        writer = make_store()
        for upstream in upstreams:
            if save_path == "add":
                await writer.add(DEFAULT_ORG_ID, upstream)
            else:
                await writer.add(DEFAULT_ORG_ID, make_bare_copy(upstream))
                await writer.update(DEFAULT_ORG_ID, upstream)
        reader = make_store()
        for upstream in upstreams:
            assert await reader.get(DEFAULT_ORG_ID, upstream.id) == upstream


@contextmanager
def record_authorization_headers() -> Iterator[tuple[str, list[list[str]]]]:
    """A loopback HTTP server that answers 401 and records the
    Authorization header(s) of every request. Yields (url, records)."""
    records: list[list[str]] = []

    class Recorder(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — stdlib handler API
            records.append(self.headers.get_all("Authorization") or [])
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 — stdlib handler API
            self.do_POST()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", records
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_saved_bearer_variable_header_reaches_upstream_substituted(
    tmp_path: Path,
) -> None:
    """An HTTP MCP saved with ``Authorization: Bearer ${API_KEY}`` must
    send the Variable's value. The load path used to copy the raw
    ``${API_KEY}`` into a token field that then overwrote the
    substituted header."""
    with record_authorization_headers() as (url, records):
        (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {
            "remote": {
                "url": url,
                "headers": {"Authorization": "Bearer ${API_KEY}"},
            },
        }}))
        (tmp_path / "config.json").write_text(json.dumps({"upstreams": {
            "remote": {"display_name": "Remote", "auth_mode": "service_account"},
        }}))
        upstream = await make_file_store_over(tmp_path).get(
            DEFAULT_ORG_ID, "remote",
        )
        assert upstream is not None
        variables = FileTemplateVarRepository(tmp_path)
        await variables.set(
            DEFAULT_ORG_ID, "remote", "API_KEY", "live-key", is_secret=True,
        )
        manager = UpstreamClientManager(
            upstreams=[upstream], template_var_repo=variables,
        )

        # The recorder answers 401, so the connect fails; only the
        # header the upstream received matters here.
        with suppress(Exception):
            await manager.connect_upstream(upstream)

    assert records, "the upstream received no request"
    assert records[0] == ["Bearer live-key"]
