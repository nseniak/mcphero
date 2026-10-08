"""Every MCP endpoint the backend mounts answers once the app has booted.

Starlette's ``Mount`` never runs a mounted app's lifespan, so the
backend's own lifespan must start each endpoint's session manager; an
endpoint it misses answers every request with 500 ("Task group is not
initialized"). The superadmin MCP was missed that way.

The real app boots here, lifespan included, and each mounted endpoint
gets a signed-in ``initialize`` with the public Host that production
nginx forwards: standalone mode for the gateway, admin and demo
endpoints; cloud mode (against a throwaway Mongo database, skipped when
none is reachable) for the superadmin endpoint, which only cloud mode
mounts. ``tests/e2e/45-admin-mcp-reachable.spec.ts`` checks the same at
the full stack. The lifespan runs in the test's own event loop on the
main thread, because it registers a SIGTERM handler, which
``TestClient``'s worker thread is not allowed to do.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit._admin_mcp_harness import (
    ADMIN_EMAIL,
    SUPERADMIN_EMAIL,
    make_initialize_body,
    make_signed_in_headers,
)
from tests.unit.mongo_fixture import require_mongo, temp_mongo_database
from tests.unit.factories import make_config_users_accepted

# Standalone URLs carry no org slug: the org-context middleware adds the
# default org's.
STANDALONE_ENDPOINT_PATHS: dict[str, str] = {
    "gateway": "/mcp/",
    "admin": "/admin-mcp/",
    "demo": "/dev/mcp-demo/mcp",
}


def make_standalone_settings(tmp_path: Path) -> Settings:
    config = {
        "roles": {
            "admin": {"is_admin": True},
            "user": {"is_default": True},
        },
        "users": {ADMIN_EMAIL: {"role": "admin"}},
    }
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
        server_url="http://localhost:8000",
        demo_mount=True,
        demo_seed=False,
    )


def make_cloud_settings(mongo_uri: str, mongo_db_name: str) -> Settings:
    """Cloud mode with the dev-stub sign-in and the local sandbox runner
    named explicitly, both of which the startup check allows only with
    test mode on a loopback bind. Nothing on this path reaches Redis, so
    it points at a closed port, never a shared one."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mode="cloud",
        host="127.0.0.1",
        test_mode=True,
        oauth_provider="dev_stub",
        mongo_uri=mongo_uri,
        mongo_db_name=mongo_db_name,
        redis_url="redis://127.0.0.1:1/0",
        sandbox_provider="local-subprocess",
        e2b_api_key="",
        session_secret="test-session-secret-cloud",
        encryption_key="test-encryption-key",
        superadmin_emails=SUPERADMIN_EMAIL,
        server_url="http://localhost:8000",
        google_client_id="",
        google_client_secret="",
    )


async def post_initialize(app: FastAPI, path: str, token: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcphero.io",
    ) as client:
        return await client.post(
            path,
            headers=make_signed_in_headers(token),
            json=make_initialize_body(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", list(STANDALONE_ENDPOINT_PATHS))
async def test_booted_app_answers_initialize_on_every_mcp_endpoint(
    tmp_path: Path, endpoint: str,
) -> None:
    app = create_app(make_standalone_settings(tmp_path))
    provider = app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined]

    async with app.router.lifespan_context(app):
        token = await provider.mint_test_token(ADMIN_EMAIL)
        resp = await post_initialize(
            app, STANDALONE_ENDPOINT_PATHS[endpoint], token,
        )

    assert resp.status_code == 200, resp.text
    assert '"serverInfo"' in resp.text


@pytest.mark.asyncio
async def test_booted_cloud_app_answers_initialize_on_the_superadmin_mcp() -> None:
    mongo_uri = require_mongo()
    async with temp_mongo_database() as db:
        app = create_app(make_cloud_settings(mongo_uri, db.name))
        provider = app.state.mcp_gateway_oauth_provider  # type: ignore[attr-defined]

        async with app.router.lifespan_context(app):
            token = await provider.mint_test_token(SUPERADMIN_EMAIL)
            resp = await post_initialize(app, "/admin-mcp/system/", token)

    assert resp.status_code == 200, resp.text
    assert '"name":"MCP Hero Superadmin"' in resp.text
