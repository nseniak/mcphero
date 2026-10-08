"""An MCP Hero operator's dashboard tab gets an org's live events.

Operators are listed in ``MCPOLIS_SUPERADMIN_EMAILS`` and may browse an
org they are not a member of. The event stream (``GET /api/events``)
ends at once for anyone else who is not a member, so the operator check
decides whether an operator's open tab updates at all. That check
ignores ASCII letter case like every other operator check: an operator
listed as ``Ops@Example.com`` who signs in as ``ops@example.com`` gets
the events, and so does one listed as ``ops@example.com`` who signs in
as ``Ops@Example.com``.

The app is the real standalone app built by ``create_app`` from the
setting as an operator writes it. The test calls the app's own
``/api/events`` handler with the signed-in address, the way the route
does after sign-in, and reads its stream.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from starlette.routing import Route

from mcpolis.domain.model.events import Event
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.event_stream import EventStream
from mcpolis.entrypoints.app import create_app
from mcpolis.entrypoints.config import Settings
from tests.unit.factories import make_config_users_accepted

ADMIN = "admin@example.com"
OPERATOR_SIGN_IN = "ops@example.com"


def make_settings(tmp_path: Path, superadmin_emails: str) -> Settings:
    """Standalone app with one admin; the operator is not a member."""
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "upstreams": {},
        "roles": {"admin": {"is_admin": True}, "user": {"is_default": True}},
        "users": {ADMIN: {"role": "admin"}},
    }))
    data_dir = tmp_path / "data"
    make_config_users_accepted(data_dir, config.read_text())
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_json_path=mcp_json,
        config_path=config,
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        oauth_provider="dev_stub",
        google_client_id="",
        google_client_secret="",
        session_secret="test-session-secret",
        server_url="http://localhost:8000",
        superadmin_emails=superadmin_emails,
    )


def events_handler(app: FastAPI) -> Callable[..., Any]:
    for route in app.routes:
        if isinstance(route, Route) and route.path == "/api/events":
            return route.endpoint
    raise AssertionError("no /api/events route")


async def frames_seen_by(app: FastAPI, email: str) -> list[str]:
    """Open ``email``'s tab, publish one org event, and return what the
    tab received before its stream ended or the event arrived."""
    response = cast(
        StreamingResponse, await events_handler(app)(email=email),
    )
    frames = cast(AsyncIterator[str], response.body_iterator)
    bus = cast(EventStream, app.state.event_bus)
    received: list[str] = []

    async def read() -> None:
        async for frame in frames:
            received.append(frame)
            return

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)  # let the tab subscribe (or end)
    bus.publish(DEFAULT_ORG_ID, Event(type="policy_changed"))
    async with asyncio.timeout(5):
        await reader
    return received


@pytest.mark.parametrize(
    ("listed", "signed_in"),
    [
        ("Ops@Example.com", OPERATOR_SIGN_IN),
        (OPERATOR_SIGN_IN, "Ops@Example.com"),
        (OPERATOR_SIGN_IN, OPERATOR_SIGN_IN),
    ],
    ids=["list-has-capitals", "sign-in-has-capitals", "same-spelling"],
)
async def test_operator_tab_gets_the_orgs_events_whatever_the_letter_case(
    tmp_path: Path, listed: str, signed_in: str,
) -> None:
    app = create_app(make_settings(tmp_path, listed))

    frames = await frames_seen_by(app, signed_in)

    assert [f.split("\n", 1)[0] for f in frames] == ["event: policy_changed"]


async def test_non_member_off_the_operator_list_gets_no_events(
    tmp_path: Path,
) -> None:
    app = create_app(make_settings(tmp_path, "someone-else@example.com"))

    assert await frames_seen_by(app, OPERATOR_SIGN_IN) == []
