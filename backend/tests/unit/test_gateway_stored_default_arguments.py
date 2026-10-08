"""Stored default arguments are checked against the role's argument
patterns on every way a tool call can enter the gateway.

The gateway checks the arguments the upstream will actually receive:
the caller's, with the upstream's stored defaults merged on top (a
default wins). These tests cover the paths ``test_gateway_controller``
does not: the multi-org gateway, the bare-name widget fallback, the
``require`` mode, the wording of a default-caused denial, and the
guard that nothing runs between the check and the merge.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp import types as mcp_types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from mcpolis.domain.model.settings import (
    ArgumentConstraint,
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.ports import MULTI_ORG_SENTINEL
from mcpolis.domain.services.org_service import OrgService
from mcpolis.domain.services.policy_engine import PolicyDecision, PolicyEngine
from mcpolis.domain.services.tool_router import ToolRouter
from mcpolis.entrypoints.controllers.gateway_controller import (
    create_mcp_server,
    current_org_id,
)
from tests.unit.factories import make_runtime_manager, make_upstream_definition
from tests.unit.test_gateway_controller import (
    make_gateway_components,
    make_router_with_github_defaults,
)
from tests.unit.test_multi_org_gateway import (
    InMemoryOrgRepo,
    make_membership,
    make_org,
)


USER = "alice@test.com"


def make_constrained_config(
    user: str, arg: str, constraint: ArgumentConstraint,
) -> SettingsConfig:
    """One role with ``github`` enabled and one argument constraint on
    ``create_issue``."""
    return SettingsConfig(
        roles={
            "default": RoleDefinition(
                is_default=True,
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={"github": True}),
                    argument_constraints={
                        "github__create_issue": {arg: constraint},
                    },
                ),
            ),
        },
        users={user: UserDefinition(role="default")},
    )


def make_forbidding_config(user: str) -> SettingsConfig:
    return make_constrained_config(
        user, "title", ArgumentConstraint(pattern="blocked", mode="forbid"),
    )


def make_requiring_config(pattern: str) -> SettingsConfig:
    return make_constrained_config(
        "anonymous", "title", ArgumentConstraint(pattern=pattern, mode="allow"),
    )


def make_org_service() -> OrgService:
    org_repo = InMemoryOrgRepo(
        orgs=[make_org("acme-id", "acme", "Acme")],
        memberships=[make_membership("acme-id", USER)],
    )
    return OrgService(org_repo=org_repo, config_repo=AsyncMock())  # type: ignore[arg-type]


async def call_single_org(
    tmp_path: Path,
    config: SettingsConfig,
    *,
    name: str,
    defaults: dict[str, Any],
    caller_args: dict[str, Any],
) -> tuple[mcp_types.CallToolResult, AsyncMock]:
    registry, router, _ = make_gateway_components(tmp_path)
    session = make_router_with_github_defaults(router, defaults)
    rm = make_runtime_manager(
        PolicyEngine(config), tool_registry=registry, tool_router=router,
    )
    handler = create_mcp_server(rm).request_handlers[mcp_types.CallToolRequest]
    result = await handler(mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=caller_args),
    ))
    return cast(mcp_types.CallToolResult, cast(Any, result).root), session


async def call_multi_org(
    tmp_path: Path, *, name: str, default_title: str, caller_title: str,
) -> tuple[mcp_types.CallToolResult, AsyncMock]:
    registry, router, _ = make_gateway_components(tmp_path)
    session = make_router_with_github_defaults(router, {"title": default_title})
    rm = make_runtime_manager(
        PolicyEngine(make_forbidding_config(USER)),
        tool_registry=registry, tool_router=router, org_id="acme-id",
    )
    server = create_mcp_server(rm, org_service=make_org_service())
    handler = server.request_handlers[mcp_types.CallToolRequest]
    auth_token = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="t", client_id=USER, scopes=[], expires_at=int(time.time()) + 3600,
    )))
    org_token = current_org_id.set(MULTI_ORG_SENTINEL)
    try:
        result = await handler(mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name=name, arguments={"title": caller_title}),
        ))
    finally:
        current_org_id.reset(org_token)
        auth_context_var.reset(auth_token)
    return cast(mcp_types.CallToolResult, cast(Any, result).root), session


def text_of(result: mcp_types.CallToolResult) -> str:
    return cast(mcp_types.TextContent, result.content[0]).text


@pytest.mark.asyncio
async def test_multi_org_forbidden_default_is_denied(tmp_path: Path) -> None:
    result, session = await call_multi_org(
        tmp_path, name="acme__github__create_issue",
        default_title="blocked by default", caller_title="fine",
    )
    assert result.isError
    assert "forbidden pattern" in text_of(result)
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_org_default_replaces_a_forbidden_caller_value(
    tmp_path: Path,
) -> None:
    result, session = await call_multi_org(
        tmp_path, name="acme__github__create_issue",
        default_title="safe default", caller_title="blocked by caller",
    )
    assert not result.isError
    sent = session.call_tool.await_args
    assert sent is not None and "safe default" in repr(sent)


@pytest.mark.asyncio
async def test_multi_org_bare_name_forbidden_default_is_denied(
    tmp_path: Path,
) -> None:
    result, session = await call_multi_org(
        tmp_path, name="create_issue",
        default_title="blocked by default", caller_title="fine",
    )
    assert result.isError
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_org_bare_name_forbidden_default_is_denied(
    tmp_path: Path,
) -> None:
    result, session = await call_single_org(
        tmp_path, make_forbidding_config("anonymous"), name="create_issue",
        defaults={"title": "blocked by default"}, caller_args={"title": "fine"},
    )
    assert result.isError
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_pattern_checks_the_default_when_caller_omits_it(
    tmp_path: Path,
) -> None:
    result, session = await call_single_org(
        tmp_path, make_requiring_config("^safe"), name="github__create_issue",
        defaults={"title": "unsafe"}, caller_args={},
    )
    assert result.isError
    assert "required pattern" in text_of(result)
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_pattern_satisfied_by_the_default_when_caller_omits_it(
    tmp_path: Path,
) -> None:
    result, session = await call_single_org(
        tmp_path, make_requiring_config("^safe"), name="github__create_issue",
        defaults={"title": "safe-x"}, caller_args={},
    )
    assert not result.isError
    assert "safe-x" in repr(session.call_tool.await_args)


@pytest.mark.asyncio
async def test_denial_caused_by_a_stored_default_says_so(tmp_path: Path) -> None:
    """The caller sent an allowed value; the stored default is what the
    role forbids. The message must say so, or the caller retries a call
    that can never pass and the admin can't tell which value is wrong."""
    result, _ = await call_single_org(
        tmp_path, make_forbidding_config("anonymous"),
        name="github__create_issue",
        defaults={"title": "blocked by default"}, caller_args={"title": "fine"},
    )
    assert result.isError
    assert "stored default" in text_of(result)


@pytest.mark.asyncio
async def test_denial_caused_by_the_caller_does_not_blame_a_default(
    tmp_path: Path,
) -> None:
    result, _ = await call_single_org(
        tmp_path, make_forbidding_config("anonymous"),
        name="github__create_issue",
        defaults={"labels": "bug"}, caller_args={"title": "blocked by caller"},
    )
    assert result.isError
    assert "stored default" not in text_of(result)


# --- Guard: nothing may run between the check and the merge ----------


def make_policy_engine_that_edits_defaults_after_allowing(
    config: SettingsConfig, router: ToolRouter,
) -> PolicyEngine:
    """A policy engine that, the moment it allows a call, schedules an
    admin edit of the stored defaults (to a forbidden value) for the
    next event-loop switch."""
    engine = PolicyEngine(config)
    decide = engine.decide_tool_call

    def edit_defaults() -> None:
        router.register_upstream(make_upstream_definition(
            id="github",
            default_arguments={"create_issue": {"title": "blocked by default"}},
        ))

    def decide_then_schedule_edit(*args: Any, **kwargs: Any) -> PolicyDecision:
        decision = decide(*args, **kwargs)
        if decision.allowed:
            asyncio.get_running_loop().call_soon(edit_defaults)
        return decision

    engine.decide_tool_call = decide_then_schedule_edit  # type: ignore[method-assign]
    return engine


@pytest.mark.asyncio
async def test_checked_default_is_the_sent_default(tmp_path: Path) -> None:
    """The gateway checks the merged arguments, then the router merges
    again before sending. That is only safe while no event-loop switch
    happens in between: an await added there would let an edit of the
    defaults land after the check, and send a default nobody checked.
    This test fails if such an await appears."""
    registry, router, _ = make_gateway_components(tmp_path)
    session = make_router_with_github_defaults(router, {"title": "safe default"})
    rm = make_runtime_manager(
        make_policy_engine_that_edits_defaults_after_allowing(
            make_forbidding_config("anonymous"), router,
        ),
        tool_registry=registry, tool_router=router,
    )
    handler = create_mcp_server(rm).request_handlers[mcp_types.CallToolRequest]

    await handler(mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="github__create_issue", arguments={"title": "fine"}),
    ))

    sent = session.call_tool.await_args
    assert sent is not None
    assert "safe default" in repr(sent)
    assert "blocked" not in repr(sent)
