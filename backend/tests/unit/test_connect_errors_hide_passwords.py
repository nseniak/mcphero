"""A connect or tool-discovery error never shows a password Variable.

Such an error often quotes the URL or command it failed on with the
Variables filled in (httpx: ``Client error '401 Unauthorized' for url
'https://...?key=<password>'``; the local runner: ``Command '/opt/<pw>/x'
not found``). The text is saved as the MCP's connection error, written
to the audit log and sent back to whoever clicked Start or Connect, so
it would show a write-only password to every admin, to an operator, and
on a member's own Connect to the member.

``UpstreamClientManager`` remembers the password values it substituted
into each MCP; ``hide_secrets_in_error`` hides them (as typed and
URL-encoded) plus what ``secret_scanner.hide_secrets_in_text`` finds.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from mcp import ClientSession

from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.repositories.file_template_var_repository import (
    FileTemplateVarRepository,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    UpstreamClientManager,
)
from mcpolis.domain.model.template_var import TemplateVarSummary
from mcpolis.domain.model.policy import AuthMode, UpstreamAuthConfig
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    TransportType,
    UpstreamDefinition,
)
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.secret_scanner import (
    HIDDEN_VALUE,
    hide_secrets_in_error,
)
from mcpolis.domain.services.upstream_admin_service import (
    StartResult,
    UpstreamAdminService,
)
from mcpolis.domain.services.upstream_connection_service import (
    _finalize_silent_refresh,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.test_admin_mcp import ADMIN_EMAIL, make_admin_parts

# Not key-shaped and under a harmless parameter name, so only the
# known-value hiding can find it.
PASSWORD = "correct horse battery"
PLAIN = "plain-region-eu-west"


def make_http_upstream(url: str) -> UpstreamDefinition:
    return UpstreamDefinition(
        id="web",
        display_name="Web",
        transport=TransportType.streamable_http,
        http=HttpTransportConfig(url=url),
        auth=UpstreamAuthConfig(mode=AuthMode.per_user_oauth),
    )


async def make_vars(tmp_path: Path, upstream_id: str) -> FileTemplateVarRepository:
    repo = FileTemplateVarRepository(tmp_path / "vars")
    await repo.set(
        DEFAULT_ORG_ID, upstream_id, "PW", PASSWORD, is_secret=True,
    )
    await repo.set(
        DEFAULT_ORG_ID, upstream_id, "REGION", PLAIN, is_secret=False,
    )
    return repo


def make_status_error_text(url: str) -> str:
    """What httpx says when the upstream refuses ``url``."""
    request = httpx.Request("GET", url)
    try:
        httpx.Response(401, request=request).raise_for_status()
    except httpx.HTTPStatusError as exc:
        return str(exc)
    raise AssertionError("a 401 always raises")


def test_hide_secrets_in_error_hides_known_values_typed_and_url_encoded() -> None:
    text = (
        f"failed on https://h/mcp?q={PASSWORD} and "
        "https://h/mcp?q=correct%20horse%20battery and "
        "https://h/mcp?q=correct+horse+battery"
    )
    hidden = hide_secrets_in_error(text, [PASSWORD])
    assert "correct" not in hidden
    assert hidden.count(HIDDEN_VALUE) == 3


def test_hide_secrets_in_error_keeps_short_values_and_still_hides_by_shape() -> None:
    hidden = hide_secrets_in_error(
        "for url 'https://bob:hunter2@h/mcp' at step ab", ["ab"],
    )
    assert "hunter2" not in hidden
    assert hidden.endswith("at step ab")


@pytest.mark.asyncio
async def test_client_manager_hides_the_passwords_it_substituted(
    tmp_path: Path,
) -> None:
    upstream = make_http_upstream(
        "https://mcp.example.com/mcp?q=${PW}&region=${REGION}",
    )
    manager = UpstreamClientManager(
        [upstream], template_var_repo=await make_vars(tmp_path, "web"),
    )
    resolved = await manager._resolve_upstream_template_vars(upstream)  # pyright: ignore[reportPrivateUsage]
    assert resolved.http is not None
    error = make_status_error_text(resolved.http.url)
    assert "correct" in error  # the leak this guards against

    shown = manager.hide_secrets_in_error("web", error)

    assert "correct" not in shown
    assert HIDDEN_VALUE in shown
    assert PLAIN in shown, "a plain Variable is shown by design"
    assert "401 Unauthorized" in shown


@pytest.mark.asyncio
async def test_start_saves_audits_and_answers_without_the_password(
    tmp_path: Path,
) -> None:
    """The local runner's "command not found" names the substituted
    command: the Start's answer, the saved connection error and the audit
    row must all hide the password inside it."""
    config: dict[str, Any] = {
        "upstreams": {
            "s0": {"display_name": "S0", "auth_mode": "service_account"},
        },
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_servers = {"s0": {"command": "/nonexistent-mcpolis/${PW}/server"}}
    manager = UpstreamClientManager(
        [], template_var_repo=await make_vars(tmp_path, "s0"),
    )
    connection_store = FileConnectionStore(tmp_path)
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager, connection_store=connection_store,
    )
    service = UpstreamAdminService(parts.action_deps)

    outcome = await service.start_upstream(
        DEFAULT_ORG_ID, "s0", actor=ADMIN_EMAIL,
    )
    started = outcome.started
    assert started is not None
    await asyncio.wait({started}, timeout=10)
    result: StartResult = started.result()

    assert result.error is not None
    assert "not found" in result.error, result.error
    assert HIDDEN_VALUE in result.error
    saved = await connection_store.get_connection_error(DEFAULT_ORG_ID, "s0")
    audit = (tmp_path / "data" / "audit.jsonl").read_text()
    for shown in (result.error, saved or "", audit):
        assert PASSWORD not in shown
    assert saved is not None and HIDDEN_VALUE in saved
    assert json.dumps(HIDDEN_VALUE)[1:-1] in audit


class ListToolsRefused:
    """A session whose tools listing fails quoting the substituted URL."""

    def __init__(self, error: str) -> None:
        self._error = error

    async def list_tools(self) -> Any:
        raise RuntimeError(self._error)


class ConnectsThenToolsListingFails(UpstreamClientManager):
    """Start's connect works (after substituting the Variables); the tool
    discovery right after it fails the way httpx reports it."""

    def __init__(self, repo: FileTemplateVarRepository) -> None:
        super().__init__([], template_var_repo=repo)
        self.session: ListToolsRefused | None = None

    async def connect_upstream(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del bearer_token, auth
        resolved = await self._resolve_upstream_template_vars(upstream)
        assert resolved.http is not None
        self.session = ListToolsRefused(make_status_error_text(resolved.http.url))
        return self.session

    def get_session(
        self, upstream_id: str, user_id: str | None = None,
    ) -> ClientSession:
        del upstream_id, user_id
        return cast(ClientSession, self.session)


@pytest.mark.asyncio
async def test_start_whose_tool_discovery_fails_answers_without_the_password(
    tmp_path: Path,
) -> None:
    config: dict[str, Any] = {
        "upstreams": {
            "web": {"display_name": "Web", "auth_mode": "service_account"},
        },
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_servers = {"web": {"url": "https://mcp.example.com/mcp?q=${PW}"}}
    manager = ConnectsThenToolsListingFails(await make_vars(tmp_path, "web"))
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager,
    )
    service = UpstreamAdminService(parts.action_deps)

    outcome = await service.start_upstream(
        DEFAULT_ORG_ID, "web", actor=ADMIN_EMAIL,
    )
    started = outcome.started
    assert started is not None
    await asyncio.wait({started}, timeout=10)
    result: StartResult = started.result()

    assert result.error is None
    shown = result.discovery_error
    assert shown is not None and "401 Unauthorized" in shown, shown
    assert HIDDEN_VALUE in shown
    assert "correct" not in shown


class ConnectCrash(BaseException):
    """Not an ``Exception``: the Start's connect step doesn't catch it, so
    the Start's task ends with it ("upstream.admin.start.crashed")."""


class CrashesAfterSubstitution(UpstreamClientManager):
    """Start's connect substitutes the Variables, then crashes with an
    error quoting the filled-in URL."""

    async def connect_upstream(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del bearer_token, auth
        resolved = await self._resolve_upstream_template_vars(upstream)
        assert resolved.http is not None
        raise ConnectCrash(make_status_error_text(resolved.http.url))


@pytest.mark.asyncio
async def test_start_that_crashes_answers_without_the_password(
    tmp_path: Path,
) -> None:
    config: dict[str, Any] = {
        "upstreams": {
            "web": {"display_name": "Web", "auth_mode": "service_account"},
        },
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_servers = {"web": {"url": "https://mcp.example.com/mcp?q=${PW}"}}
    manager = CrashesAfterSubstitution(
        [], template_var_repo=await make_vars(tmp_path, "web"),
    )
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager,
    )
    service = UpstreamAdminService(parts.action_deps)

    outcome = await service.start_upstream(
        DEFAULT_ORG_ID, "web", actor=ADMIN_EMAIL,
    )
    started = outcome.started
    assert started is not None
    await asyncio.wait({started}, timeout=10)
    result: StartResult = started.result()

    shown = result.error
    assert shown is not None and "401 Unauthorized" in shown, shown
    assert HIDDEN_VALUE in shown
    assert "correct" not in shown


class FailsAfterSubstitution(UpstreamClientManager):
    """A sign-in's session that substitutes the Variables, then is
    refused the way httpx reports it: with the full URL."""

    async def replace_user_session(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        *,
        auth: httpx.Auth | None = None,
        bearer_token: str | None = None,
    ) -> Any:
        del user_id, auth, bearer_token
        resolved = await self._resolve_upstream_template_vars(upstream)
        assert resolved.http is not None
        raise RuntimeError(make_status_error_text(resolved.http.url))


@pytest.mark.asyncio
async def test_sign_in_whose_connect_fails_answers_without_the_password(
    tmp_path: Path,
) -> None:
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    manager = FailsAfterSubstitution(
        [upstream], template_var_repo=await make_vars(tmp_path, "web"),
    )

    result = await _finalize_silent_refresh(
        manager,
        PendingAuthCoordinator(b"k" * 32),
        MagicMock(),
        DEFAULT_ORG_ID,
        upstream,
        ADMIN_EMAIL,
    )

    assert result.error is not None
    assert result.error.startswith(
        "Authentication succeeded but the connection to this MCP failed:",
    )
    assert "401 Unauthorized" in result.error
    assert "correct" not in result.error


# ── Spellings, rotations and failures (independent review) ─────────────


@pytest.mark.parametrize("url_template", [
    "https://h.example.com/mcp?tenant={}",
    "https://h.example.com/{}/mcp",
])
@pytest.mark.parametrize("password", ["ab cd/ef", "x y:z@w", 'a"b c'])
def test_hide_secrets_in_error_hides_the_password_as_httpx_encodes_it(
    url_template: str, password: str,
) -> None:
    """httpx escapes a space but keeps ``/ : @``, which neither
    ``quote(safe="")`` nor ``quote_plus`` gives."""
    text = make_status_error_text(url_template.format(password))
    shown = hide_secrets_in_error(text, [password])
    assert HIDDEN_VALUE in shown, shown
    assert re.search(r"(ab|x|a)(%20|%22)", shown) is None, shown


@pytest.mark.parametrize(
    "password", ["pa\\ss-word", "pass\tword", "it's \"quoted\""],
)
def test_hide_secrets_in_error_hides_a_repr_escaped_password(
    password: str,
) -> None:
    """An ``OSError`` quotes its path with ``repr``: backslashes doubled,
    a tab written as ``\\t``, a ``'`` escaped when both quotes are in it."""
    text = str(FileNotFoundError(
        2, "No such file or directory", f"/opt/{password}/server",
    ))
    shown = hide_secrets_in_error(text, [password])
    assert shown == (
        f"[Errno 2] No such file or directory: '/opt/{HIDDEN_VALUE}/server'"
    )


def test_hide_secrets_in_error_hides_a_json_escaped_password() -> None:
    password = 'pa"ss-word'
    text = json.dumps({"command": f"/opt/{password}/x"})
    assert hide_secrets_in_error(text, [password]) == json.dumps(
        {"command": f"/opt/{HIDDEN_VALUE}/x"},
    )


def test_hide_secrets_in_error_hides_a_lower_cased_host_name() -> None:
    password = "TenantKEY9"
    text = make_status_error_text(f"https://{password}.example.com/mcp")
    assert "tenantkey9" not in hide_secrets_in_error(text, [password]).lower()


async def make_resolved_error(
    manager: UpstreamClientManager, upstream: UpstreamDefinition,
) -> str:
    resolved = await manager._resolve_upstream_template_vars(upstream)  # pyright: ignore[reportPrivateUsage]
    assert resolved.http is not None
    return make_status_error_text(resolved.http.url)


@pytest.mark.asyncio
async def test_a_rotated_password_stays_hidden_for_the_old_session(
    tmp_path: Path,
) -> None:
    """A save never closes a running session, so its errors still quote
    the old value after another session substituted the new one."""
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    repo = await make_vars(tmp_path, "web")
    manager = UpstreamClientManager([upstream], template_var_repo=repo)
    old_error = await make_resolved_error(manager, upstream)
    await repo.set(
        DEFAULT_ORG_ID, "web", "PW", "rotated new value", is_secret=True,
    )
    await make_resolved_error(manager, upstream)

    assert "correct" not in manager.hide_secrets_in_error("web", old_error)


@pytest.mark.asyncio
async def test_a_password_edited_out_of_the_url_stays_hidden(
    tmp_path: Path,
) -> None:
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    manager = UpstreamClientManager(
        [upstream], template_var_repo=await make_vars(tmp_path, "web"),
    )
    old_error = await make_resolved_error(manager, upstream)
    await manager._resolve_upstream_template_vars(  # pyright: ignore[reportPrivateUsage]
        make_http_upstream("https://mcp.example.com/mcp"),
    )

    assert "correct" not in manager.hide_secrets_in_error("web", old_error)


class SummariesDown(FileTemplateVarRepository):
    """Values readable, password flags not (a Mongo hiccup)."""

    async def list_summaries(
        self, org_id: str, upstream_id: str,
    ) -> list[TemplateVarSummary]:
        raise RuntimeError("mongo down")


@pytest.mark.asyncio
async def test_without_the_password_flags_every_variable_is_hidden(
    tmp_path: Path,
) -> None:
    repo = SummariesDown(tmp_path / "vars")
    await repo.set(DEFAULT_ORG_ID, "web", "PW", PASSWORD, is_secret=True)
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    manager = UpstreamClientManager([upstream], template_var_repo=repo)
    error = await make_resolved_error(manager, upstream)

    assert "correct" not in manager.hide_secrets_in_error("web", error)


@pytest.mark.asyncio
async def test_removing_an_mcp_forgets_its_passwords(tmp_path: Path) -> None:
    """The same id added again is a new MCP: its errors must not be
    hidden with, nor the process keep, the removed one's passwords."""
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    manager = UpstreamClientManager(
        [upstream], template_var_repo=await make_vars(tmp_path, "web"),
    )
    await make_resolved_error(manager, upstream)
    await manager.unregister_upstream("web")

    assert manager.hide_secrets_in_error("web", PASSWORD) == PASSWORD


class StopFailsQuotingTheUrl(UpstreamClientManager):
    """Start connects (substituting the Variables); a Stop's disconnect
    then fails with an error quoting the filled-in URL."""

    def __init__(self, repo: FileTemplateVarRepository) -> None:
        super().__init__([], template_var_repo=repo)
        self.error = ""

    async def connect_upstream(
        self,
        upstream: UpstreamDefinition,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> Any:
        del bearer_token, auth
        self.error = await make_resolved_error(self, upstream)
        return MagicMock()

    async def disconnect_upstream(
        self, upstream_id: str, *, reset_state: bool = True,
    ) -> None:
        del upstream_id, reset_state
        if self.error:
            raise RuntimeError(self.error)


@pytest.mark.asyncio
async def test_a_failed_stop_is_audited_without_the_password(
    tmp_path: Path,
) -> None:
    config: dict[str, Any] = {
        "upstreams": {
            "web": {"display_name": "Web", "auth_mode": "service_account"},
        },
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
    mcp_servers = {"web": {"url": "https://mcp.example.com/mcp?q=${PW}"}}
    manager = StopFailsQuotingTheUrl(await make_vars(tmp_path, "web"))
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers,
        client_manager=manager,
    )
    service = UpstreamAdminService(parts.action_deps)
    upstream = make_http_upstream("https://mcp.example.com/mcp?q=${PW}")
    await manager.connect_upstream(upstream)
    assert "correct" in manager.error

    with pytest.raises(RuntimeError):
        await service.stop_upstream(DEFAULT_ORG_ID, "web", actor=ADMIN_EMAIL)

    audit = (tmp_path / "data" / "audit.jsonl").read_text()
    assert "401 Unauthorized" in audit
    assert "correct" not in audit
