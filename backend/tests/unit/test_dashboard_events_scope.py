"""What a dashboard tab's event stream (``GET /api/events``) delivers.

Every event published to an org reaches every open tab of the org. The
Audit page is admin-only, so audit rows (``audit_entry`` events) must
reach only the org's admins. And a member removed from the org must
stop receiving the org's events at once, not when their tab reconnects.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from mcpolis.adapters.event_stream_inprocess import InProcessEventStream
from mcpolis.domain.model.events import Event
from mcpolis.domain.model.settings import RoleDefinition, SettingsConfig, UserDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.services.org_runtime import OrgRuntimeManager
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.entrypoints.routes.dashboard.events import dashboard_event_frames
from tests.unit.factories import make_runtime_manager

ADMIN = "admin@example.com"
MEMBER = "member@example.com"
OPERATOR = "op@mcphero.io"


def make_org_runtimes() -> tuple[OrgRuntimeManager, PolicyEngine]:
    policy = PolicyEngine(
        SettingsConfig(
            roles={
                "admin": RoleDefinition(is_admin=True),
                "user": RoleDefinition(is_default=True),
            },
            users={
                ADMIN: UserDefinition(role="admin"),
                MEMBER: UserDefinition(role="user"),
            },
        ),
        [ADMIN, MEMBER],
    )
    return make_runtime_manager(policy), policy


def open_tab(
    bus: InProcessEventStream,
    manager: OrgRuntimeManager,
    email: str,
    *,
    frames_wanted: int,
    is_superadmin: bool = False,
) -> asyncio.Task[list[str]]:
    """A tab of ``email``'s, reading until it has ``frames_wanted`` frames
    or its stream ends."""

    async def read() -> list[str]:
        frames: AsyncIterator[str] = dashboard_event_frames(
            bus, manager, DEFAULT_ORG_ID, email, is_superadmin=is_superadmin,
        )
        got: list[str] = []
        async for frame in frames:
            got.append(frame)
            if len(got) == frames_wanted:
                break
        return got

    return asyncio.create_task(read())


async def wait_until_listening(bus: InProcessEventStream, *emails: str) -> None:
    async with asyncio.timeout(5):
        while not all(e in bus._subscribers for e in emails):  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0.005)


def event_types(frames: list[str]) -> list[str]:
    return [frame.split("\n", 1)[0].removeprefix("event: ") for frame in frames]


async def test_audit_rows_reach_only_the_orgs_admins() -> None:
    bus = InProcessEventStream()
    manager, _ = make_org_runtimes()
    member_tab = open_tab(bus, manager, MEMBER, frames_wanted=1)
    admin_tab = open_tab(bus, manager, ADMIN, frames_wanted=2)
    operator_tab = open_tab(
        bus, manager, OPERATOR, frames_wanted=2, is_superadmin=True,
    )
    await wait_until_listening(bus, MEMBER, ADMIN, OPERATOR)

    bus.publish(DEFAULT_ORG_ID, Event(type="audit_entry", payload={"action": "x"}))
    bus.publish(DEFAULT_ORG_ID, Event(type="policy_changed"))

    async with asyncio.timeout(5):
        member_frames, admin_frames, operator_frames = await asyncio.gather(
            member_tab, admin_tab, operator_tab,
        )
    assert event_types(member_frames) == ["policy_changed"]
    assert event_types(admin_frames) == ["audit_entry", "policy_changed"]
    assert event_types(operator_frames) == ["audit_entry", "policy_changed"]


async def test_a_removed_members_open_stream_ends() -> None:
    bus = InProcessEventStream()
    manager, policy = make_org_runtimes()
    member_tab = open_tab(bus, manager, MEMBER, frames_wanted=10)
    await wait_until_listening(bus, MEMBER)

    # The removal: the running policy loses them, then announces it.
    policy.reload(SettingsConfig(
        roles=policy.config.roles,
        users={ADMIN: UserDefinition(role="admin")},
    ))
    policy.discard_member(MEMBER)
    bus.publish(DEFAULT_ORG_ID, Event(type="policy_changed", payload={"user": MEMBER}))

    async with asyncio.timeout(5):
        frames = await member_tab
    assert frames == []
    assert MEMBER not in bus._subscribers  # pyright: ignore[reportPrivateUsage]
