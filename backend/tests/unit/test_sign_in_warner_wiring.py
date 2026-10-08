"""The §5.2 warner reaches every org's reconnects.

A reconnect that deletes a refused sign-in warns through its org's
client manager (``UpstreamClientManager.sign_in_warner``). The warner is
built once at startup and handed to the runtime manager, which must pass
it to orgs already loaded and to orgs loaded later; an org without it
would delete sign-ins silently again.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from mcpolis.adapters.email.stub_email_sender import StubEmailSender
from mcpolis.adapters.repositories.connection_store import (
    ConnectionStore,
    OAuthToken,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit.factories import (
    make_config_users_accepted,
    make_oauth_upstream,
    make_sign_in_warner,
)


def make_org_runtime_manager() -> OrgRuntimeManager:
    return OrgRuntimeManager(
        config_repo=MagicMock(),
        upstream_config_repo=MagicMock(),
        connection_repo=MagicMock(),
        audit_repo=MagicMock(),
        tool_catalog_repo=MagicMock(),
        server_url="http://localhost:8080",
    )


def test_warner_reaches_orgs_loaded_before_and_after_it_is_set() -> None:
    manager = make_org_runtime_manager()
    loaded_before = manager.create_runtime_sync("org-a", SettingsConfig(), [])
    warner = make_sign_in_warner(StubEmailSender())

    manager.set_sign_in_warner(warner)
    loaded_after = manager.create_runtime_sync("org-b", SettingsConfig(), [])

    assert loaded_before.client_manager.sign_in_warner is warner
    assert loaded_after.client_manager.sign_in_warner is warner


def test_no_warner_while_health_emails_are_off() -> None:
    manager = make_org_runtime_manager()
    manager.set_sign_in_warner(None)

    runtime = manager.create_runtime_sync("org-a", SettingsConfig(), [])

    assert runtime.client_manager.sign_in_warner is None


ADMIN = "admin@example.com"
ORG_CONFIG_WITH_ONE_ADMIN = {
    "roles": {
        "admin": {"is_admin": True, "settings": {}},
        "developer": {"settings": {}},
    },
    "users": {ADMIN: {"role": "admin"}, "dev@example.com": {"role": "developer"}},
}


def make_standalone_app(
    tmp_path: Path,
    *,
    health_emails: bool,
    org_config: dict[str, Any] | None = None,
) -> FastAPI:
    """A standalone app (default org loaded at startup), with the §5.2
    health emails on or off. SMTP values are placeholders: nothing is
    sent while building the app."""
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (tmp_path / "config.json").write_text(json.dumps(org_config or {}))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    make_config_users_accepted(data_dir, json.dumps(org_config or {}))
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=tmp_path / "mcp.json",
        config_path=tmp_path / "config.json",
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        upstream_health_email_enabled=health_emails,
        smtp_host="smtp.example.invalid",
        smtp_username="sender@example.invalid",
        smtp_password="placeholder-password",
        smtp_from="sender@example.invalid",
    )
    with patch(
        "mcpolis.adapters.upstream_clients.client_manager.UpstreamClientManager.start_all"
    ), patch(
        "mcpolis.domain.services.tool_registry.ToolRegistry.refresh_all"
    ):
        return create_app(settings)


def test_app_with_health_emails_on_wires_the_warner(tmp_path: Path) -> None:
    app = make_standalone_app(tmp_path, health_emails=True)
    manager: OrgRuntimeManager = app.state.runtime_manager  # type: ignore[attr-defined]
    runtime = manager.get_cached(DEFAULT_ORG_ID)
    assert runtime is not None

    warner = runtime.client_manager.sign_in_warner

    assert warner is not None
    assert warner.orgs.slug(DEFAULT_ORG_ID) == DEFAULT_ORG_ID


def test_app_with_health_emails_off_has_no_warner(tmp_path: Path) -> None:
    app = make_standalone_app(tmp_path, health_emails=False)
    manager: OrgRuntimeManager = app.state.runtime_manager  # type: ignore[attr-defined]
    runtime = manager.get_cached(DEFAULT_ORG_ID)
    assert runtime is not None

    assert runtime.client_manager.sign_in_warner is None


async def test_app_org_facts_follow_the_dashboard_readiness_rule(
    tmp_path: Path,
) -> None:
    """The real app's answers to the warner: admins come from the org's
    policy config, and "an admin is still signed in" follows
    ``resolve_upstream_readiness``: only an admin's stored sign-in counts,
    a member's doesn't."""
    app = make_standalone_app(
        tmp_path, health_emails=True, org_config=ORG_CONFIG_WITH_ONE_ADMIN,
    )
    manager: OrgRuntimeManager = app.state.runtime_manager  # type: ignore[attr-defined]
    store: ConnectionStore = app.state.connection_store  # type: ignore[attr-defined]
    runtime = manager.get_cached(DEFAULT_ORG_ID)
    assert runtime is not None
    warner = runtime.client_manager.sign_in_warner
    assert warner is not None
    upstream = make_oauth_upstream(id="notion", mode=AuthMode.per_user_oauth)
    token = OAuthToken(
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=[],
    )

    assert await warner.orgs.admin_emails(DEFAULT_ORG_ID) == [ADMIN]
    assert await warner.orgs.has_admin_sign_in(DEFAULT_ORG_ID, upstream) is False

    await store.put_user_token(DEFAULT_ORG_ID, "dev@example.com", "notion", token)
    assert await warner.orgs.has_admin_sign_in(DEFAULT_ORG_ID, upstream) is False

    await store.put_user_token(DEFAULT_ORG_ID, ADMIN, "notion", token)
    assert await warner.orgs.has_admin_sign_in(DEFAULT_ORG_ID, upstream) is True
