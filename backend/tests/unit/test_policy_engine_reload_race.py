"""AUTH-16 — deleted-role mid-request reload race fails closed.

A service token's role is resolved at the auth boundary and passed to
``PolicyEngine.decide_tool_call`` as ``boundary_role``. The engine
reads ``self._config`` afresh on every call, and ``reload`` swaps that
reference atomically (a plain attribute assignment). When an admin
deletes the role a live token is pinned to, concurrent in-flight
``decide_tool_call`` requests must never observe a *torn* config — a
decision that allows against a role the new config no longer has.

This module drives many real asyncio decide-tasks interleaved with a
``reload`` to a config that drops the role, and pins:

- Every decision is internally consistent: either ALLOWED resolved
  against the old config, or DENIED (failed closed) — never an
  allow attributed to a role the live config lacks.
- No decision raises.
- Once ``reload`` has landed, every subsequent decision denies with
  ``user_not_in_any_role`` (the deleted-role-fails-closed contract).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from mcpolis.domain.model.settings import (
    McpAccessConfig,
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
)
from mcpolis.domain.services.policy_engine import PolicyEngine

UPSTREAM_ID = "demo"
TOOL_NAME = "do_thing"
ROLE = "ci-role"


def make_config_with_role() -> SettingsConfig:
    """Config where ``ci-role`` can reach ``demo`` and all its tools."""
    return SettingsConfig(
        roles={
            ROLE: RoleDefinition(
                settings=RoleSettings(
                    mcp_access=McpAccessConfig(mcps={UPSTREAM_ID: True}),
                ),
            ),
        },
    )


def make_config_without_role() -> SettingsConfig:
    """Config after the admin deletes ``ci-role`` — zero roles."""
    return SettingsConfig(roles={})


def decide(engine: PolicyEngine) -> bool:
    """Run one boundary-role tool decision; return its ``allowed``."""
    return engine.decide_tool_call(
        "svc:ci-bot",
        UPSTREAM_ID,
        TOOL_NAME,
        arguments={},
        boundary_role=ROLE,
    ).allowed


@pytest.mark.asyncio
async def test_decide_with_role_present_is_allowed() -> None:
    """Baseline: with the role present, the boundary-role decision is
    allowed — so the deny seen post-reload is caused by the deletion,
    not by a misbuilt config."""
    engine = PolicyEngine(make_config_with_role())
    assert decide(engine) is True


@pytest.mark.asyncio
async def test_concurrent_decide_with_reload_never_tears_and_fails_closed(
) -> None:
    """AUTH-16: hammer ``decide_tool_call`` from many concurrent tasks
    while a reload drops the role. Every observed decision must be
    consistent with one of the two configs (allowed under the old,
    denied under the new) — never a torn allow against the deleted
    role — and none may raise.

    NOTE on what this does and does NOT prove: the fail-closed guarantee
    holds *because* ``decide_tool_call`` and ``reload`` are synchronous —
    a decision reads ``self._config`` and finishes atomically before any
    other coroutine runs, and ``reload`` is a single attribute swap, so
    no interleaving can tear a decision. This test exercises that path
    under concurrency but cannot, by construction, observe a tear today;
    ``test_decide_and_reload_are_synchronous_so_no_tearing_window_exists``
    is the real guard — it fails the moment either method becomes async
    (opening a tearing window this reasoning would no longer cover)."""
    engine = PolicyEngine(make_config_with_role())

    reload_started = asyncio.Event()

    async def decider() -> tuple[bool, str | None]:
        # Spread the decisions across the reload by yielding first.
        await reload_started.wait()
        decision = engine.decide_tool_call(
            "svc:ci-bot",
            UPSTREAM_ID,
            TOOL_NAME,
            arguments={},
            boundary_role=ROLE,
        )
        return decision.allowed, decision.matched_role

    async def reloader() -> None:
        reload_started.set()
        # Yield so deciders interleave around the swap.
        await asyncio.sleep(0)
        engine.reload(make_config_without_role())

    decider_tasks = [asyncio.create_task(decider()) for _ in range(200)]
    reloader_task = asyncio.create_task(reloader())

    results = await asyncio.gather(*decider_tasks)
    await reloader_task

    for allowed, matched_role in results:
        if allowed:
            # An allow may only be attributed to the role that still
            # exists in the config it resolved against — never a torn
            # allow against the deleted role.
            assert matched_role == ROLE
        # else: failed closed — acceptable.

    # The swap has landed; every decision from here denies, failing
    # closed with the no-role reason.
    final = engine.decide_tool_call(
        "svc:ci-bot",
        UPSTREAM_ID,
        TOOL_NAME,
        arguments={},
        boundary_role=ROLE,
    )
    assert final.allowed is False
    assert final.reason == "user_not_in_any_role"


@pytest.mark.asyncio
async def test_decide_after_reload_to_role_less_config_denies() -> None:
    """AUTH-16 (settled state): once the role is gone, a boundary-role
    decision fails closed — the ``resolve_settings_for_role`` ->
    ``_EMPTY`` -> empty-role-name -> deny chain, end to end."""
    engine = PolicyEngine(make_config_with_role())
    assert decide(engine) is True
    engine.reload(make_config_without_role())
    decision = engine.decide_tool_call(
        "svc:ci-bot",
        UPSTREAM_ID,
        TOOL_NAME,
        arguments={},
        boundary_role=ROLE,
    )
    assert decision.allowed is False
    assert decision.reason == "user_not_in_any_role"
    # ``get_allowed_upstreams`` and ``filter_tools`` fail closed too.
    assert engine.get_allowed_upstreams(
        "svc:ci-bot", boundary_role=ROLE,
    ) == set()
    assert engine.filter_tools(
        "svc:ci-bot",
        [(UPSTREAM_ID, TOOL_NAME, {})],
        boundary_role=ROLE,
    ) == []


def test_decide_and_reload_are_synchronous_so_no_tearing_window_exists(
) -> None:
    """AUTH-16 (the load-bearing invariant): the deleted-role-mid-reload
    fail-closed guarantee rests entirely on these methods being
    SYNCHRONOUS — a decision reads ``self._config`` and returns before any
    other coroutine runs, and ``reload`` swaps the reference in one atomic
    assignment, so a concurrent reload can never tear a decision in flight.
    If any of them becomes a coroutine (an ``await`` split inside a
    decision, or an async reload), that window opens and the concurrency
    test above would no longer be sufficient. This canary fails the moment
    that happens, forcing the invariant to be re-examined."""
    engine = PolicyEngine(make_config_with_role())
    assert not inspect.iscoroutinefunction(engine.decide_tool_call)
    assert not inspect.iscoroutinefunction(engine.reload)
    assert not inspect.iscoroutinefunction(engine.get_allowed_upstreams)
    assert not inspect.iscoroutinefunction(engine.filter_tools)
