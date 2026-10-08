"""The Admin MCP's ``tools/call`` wrapper is what runs a cancelled call
to its end, not only the services behind it.

Every cancelled-call test in ``test_admin_mcp_cancelled_calls`` drives an
action whose service method is ``@runs_to_completion``, so those tests
stayed green with the wrapper removed. These two drive what only the
wrapper guards: a tool with no such service behind it
(``set_default_arguments``), and the wait after a cancel, which keeps a
closing session from waiting on an action for as long as the action
takes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ToolAnnotations

from mcpolis.adapters.repositories.file_config_store import FileConfigStore
from mcpolis.domain.model.settings import SettingsConfig, UpstreamOptions
from mcpolis.entrypoints.controllers.admin_tool_calls import (
    install_call_tool_wrapper,
)
from mcpolis.entrypoints.mcp_transport_security import mcp_transport_security
from tests.unit.factories import (
    FIRST_CALL_ID,
    Gate,
    cancel_mcp_call_while_gated,
    make_bearer_auth,
    make_cancel_notification,
)
from tests.unit.test_admin_mcp import (
    ADMIN_EMAIL,
    _call,  # pyright: ignore[reportPrivateUsage]
    _config_with_one_stdio,  # pyright: ignore[reportPrivateUsage]
    make_admin_parts,
)

_SHORT_WAIT_AFTER_CANCEL_SECONDS = 0.3
# Long enough to tell a call that ended after its short wait from one
# that waited for its action.
_ACTION_HELD_SECONDS = 3.0


class UpstreamOptionsSaveWaits(FileConfigStore):
    """Config store whose save of an MCP's options (its default arguments
    among them) waits at ``gate`` before it writes."""

    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path)
        self.gate = Gate()

    async def set_upstream_options(
        self, org_id: str, upstream_id: str, options: UpstreamOptions,
    ) -> SettingsConfig:
        await self.gate.hold()
        return await super().set_upstream_options(org_id, upstream_id, options)


def make_server_with_one_action(
    gate: Gate, saved: asyncio.Event, *, wait_after_cancel: float,
) -> FastMCP:
    """An admin-like endpoint whose one tool saves something, held at
    ``gate`` first, behind the real wrapper."""
    server = FastMCP(name="admin", transport_security=mcp_transport_security())

    @server.tool(annotations=ToolAnnotations(destructiveHint=False))
    async def save_setting() -> str:  # pyright: ignore[reportUnusedFunction]
        await gate.hold()
        saved.set()
        return "saved"

    install_call_tool_wrapper(server, None, wait_after_cancel=wait_after_cancel)
    return server


async def release_after(gate: Gate, seconds: float) -> None:
    await asyncio.sleep(seconds)
    gate.release.set()


async def test_a_cancelled_set_default_arguments_still_saves_them(
    tmp_path: Path,
) -> None:
    config, mcp_servers = _config_with_one_stdio()
    store = UpstreamOptionsSaveWaits(tmp_path / "config.json")
    parts = await make_admin_parts(
        tmp_path, config=config, mcp_servers=mcp_servers, config_store=store,
    )

    await cancel_mcp_call_while_gated(
        parts.server, store.gate,
        "set_default_arguments", {
            "mcp_id": "s0", "tool_name": "search",
            "arguments_json": '{"org": "acme"}',
        },
        caller=ADMIN_EMAIL,
    )

    listed = json.loads(
        await _call(parts.server, "list_default_arguments", {"mcp_id": "s0"}),
    )
    assert listed == {"default_arguments": {"search": {"org": "acme"}}}


async def test_a_cancelled_call_ends_after_its_wait_and_its_action_goes_on() -> None:
    """The session closes once the call's wait after the cancel is over,
    while its action is still held; the action finishes later."""
    gate, saved = Gate(), asyncio.Event()
    server = make_server_with_one_action(
        gate, saved, wait_after_cancel=_SHORT_WAIT_AFTER_CANCEL_SECONDS,
    )
    loop = asyncio.get_running_loop()
    closing_started = closing_took = 0.0
    saved_when_closed = True
    auth = auth_context_var.set(make_bearer_auth(ADMIN_EMAIL))
    try:
        async with asyncio.timeout(10):
            async with create_connected_server_and_client_session(
                server,
            ) as client:
                call = asyncio.create_task(client.call_tool("save_setting", {}))
                await gate.reached.wait()
                # Without the wait, the session would close only once the
                # action ends; never hang the test on it.
                releasing = asyncio.create_task(
                    release_after(gate, _ACTION_HELD_SECONDS),
                )
                await client.send_notification(
                    make_cancel_notification(FIRST_CALL_ID),
                )
                with contextlib.suppress(McpError):  # "Request cancelled"
                    await call
                closing_started = loop.time()
            # Leaving the session waits for the server's handlers to end.
            closing_took = loop.time() - closing_started
            saved_when_closed = saved.is_set()
            await asyncio.wait_for(saved.wait(), _ACTION_HELD_SECONDS + 2)
            await releasing
    finally:
        auth_context_var.reset(auth)

    assert closing_took < _ACTION_HELD_SECONDS - 1, (
        f"the session took {closing_took:.1f} s to close: the cancelled call "
        "waited for its action instead of its short wait"
    )
    assert not saved_when_closed
