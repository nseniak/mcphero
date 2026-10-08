"""The dev demo MCP gets role access like every other new MCP.

The boot seed adds the bundled demo upstream when it is missing. It used
to skip the per-role access entries every other add creates, and since
removing an MCP now drops its role rules, a removed-then-reseeded demo
came back usable by no role. The seed now goes through the same
``grant_role_access`` step as the shared admin add, which also drops any
rule still stored under the demo's id.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.model.settings import SettingsConfig, ToolAccessConfig
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._admin_mcp_harness import ADMIN_EMAIL
from tests.unit.factories import make_config_users_accepted

DEMO_ID = "mcp-demo"
FRESH_TOOL_ACCESS = ToolAccessConfig(
    fallback_enabled=True,
    category_defaults={"readOnly": True, "destructive": True},
)


def make_config_with_orphan_demo_rules() -> dict[str, Any]:
    """Role "user" still holds a deny-everything-but-one rule for the
    demo's id, left by a remove made before removal purged role rules."""
    return {
        "roles": {
            "admin": {"is_admin": True,
                      "settings": {"mcp_access": {"auto_enable_new": True}}},
            "user": {"is_default": True, "settings": {
                "mcp_access": {"auto_enable_new": True,
                               "mcps": {DEMO_ID: False}},
                "tool_access": {DEMO_ID: {"tools": {"echo": True}}},
            }},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }


def make_seeding_settings(tmp_path: Path) -> Settings:
    config = make_config_with_orphan_demo_rules()
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {}}))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    make_config_users_accepted(tmp_path / "data", json.dumps(config))
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_path,
        config_path=config_path,
        data_dir=tmp_path / "data",
        audit_log_path=tmp_path / "data" / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        # Nothing listens here: the seed's connect fails, is logged, and
        # the seed carries on, as at a dev boot without the demo up.
        server_url="http://127.0.0.1:1",
        demo_mount=True,
        demo_seed=True,
    )


async def wait_for_demo_access(config_path: Path) -> SettingsConfig:
    store = FileConfigStore(config_path)
    for _ in range(100):
        config = await store.load(DEFAULT_ORG_ID)
        if DEMO_ID in config.roles["admin"].settings.tool_access:
            return config
        await asyncio.sleep(0.1)
    raise AssertionError("the seeded demo never got role access")


@pytest.mark.asyncio
async def test_seeded_demo_gets_fresh_role_access(tmp_path: Path) -> None:
    settings = make_seeding_settings(tmp_path)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        config = await wait_for_demo_access(settings.config_path)
        live = app.state.runtime_manager  # type: ignore[attr-defined]
        runtime = await live.get(DEFAULT_ORG_ID)
        live_config = runtime.policy_engine.config

    for seen in (config, live_config):
        for role in seen.roles.values():
            assert role.settings.mcp_access.mcps[DEMO_ID] is True
            assert role.settings.tool_access[DEMO_ID] == FRESH_TOOL_ACCESS
