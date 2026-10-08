"""The one ``tools/call`` handler of the two admin MCP endpoints: an org's
Admin MCP and the operator MCP (``/admin-mcp/system``).

Both change several stores in a row from a tool call, and an AI client
can cancel the call (its user pressed Esc) or close its session at any
point. Cut half-way, an action leaves the stores disagreeing: the
operator's org deletion removed the org, then stopped before it revoked
the org's service tokens, which kept working and could no longer be
revoked. So every tool that changes something runs to its end here,
whichever endpoint it is on.
"""
# NOTE: no `from __future__ import annotations`, like the controllers that
# register FastMCP tools.

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock, TextContent

from mcpolis.domain.services.cancel_shield import finish_despite_cancels
from mcpolis.domain.services.rate_limit_service import RateLimitService
from mcpolis.entrypoints.controllers.gateway_controller import (
    current_caller_id,
    current_org_id,
)

# Once a tool call is cancelled (the client's ``notifications/cancelled``,
# or its session closing), how long the call still waits for the action
# before it ends. The action runs to its end either way; this only bounds
# how long a closing session, at a deploy's shutdown say, waits for a
# call that is merely waiting for an outcome, like a Start's 45 s.
WAIT_AFTER_CANCEL_SECONDS = 10.0


def install_call_tool_wrapper(
    server: FastMCP,
    rate_limits: RateLimitService | None,
    *,
    wait_after_cancel: float = WAIT_AFTER_CANCEL_SECONDS,
) -> None:
    """Make every tool call of ``server`` go through one handler.

    FastMCP registered its own low-level ``tools/call`` handler at
    construction; this re-registers it with a wrapper that:

    - charges the call to the calling admin, when rate limits are on
      (the operator MCP has none). A refusal is an ``isError`` result,
      like the gateway's, so the AI client reads the wait instead of
      seeing a broken connection;
    - runs every tool that changes something (any tool not annotated
      read-only) to its end, even when the client cancels the call
      (``notifications/cancelled``) or the session closes meanwhile; see
      ``cancel_shield``. The cancel is passed on once the tool has ended
      (or after ``wait_after_cancel`` seconds, the tool going on in the
      background, where the shutdown waits for it), so the SDK, which
      already answered "Request cancelled", does not answer a second
      time: that second answer fails with "Request already responded to"
      and drops the whole session.

    Don't add a per-tool ``anyio.CancelScope(shield=True)`` instead: a
    handler that returns normally after the client cancelled makes the SDK
    answer twice, as above.
    """
    fastmcp_call_tool = server.call_tool
    low_level_server = server._mcp_server  # pyright: ignore[reportPrivateUsage]
    read_only_tools: set[str] = set()
    listed = False

    async def is_read_only(name: str) -> bool:
        nonlocal listed
        if not listed:
            read_only_tools.update(
                tool.name for tool in await server.list_tools()
                if tool.annotations is not None
                and tool.annotations.readOnlyHint is True
            )
            listed = True
        return name in read_only_tools

    @low_level_server.call_tool(validate_input=False)
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any] | CallToolResult:
        if rate_limits is not None:
            refusal = await rate_limits.admit_admin_mcp_call(
                user=current_caller_id(), org_id=current_org_id.get(),
            )
            if refusal is not None:
                return CallToolResult(
                    content=[TextContent(type="text", text=refusal.message)],
                    isError=True,
                )
        if await is_read_only(name):
            return await fastmcp_call_tool(name, arguments)
        return await finish_despite_cancels(
            fastmcp_call_tool(name, arguments),
            name=f"tools/call {name}",
            wait_after_cancel=wait_after_cancel,
        )
