"""Shared MCP client message handlers for upstream connections."""
from __future__ import annotations

from collections.abc import Callable

import mcp.types as mcp_types
import structlog
from mcp.client.session import MessageHandlerFnT
from mcp.shared.session import RequestResponder

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

OnToolListChanged = Callable[[], None]
OnResourceListChanged = Callable[[], None]
OnPromptListChanged = Callable[[], None]


def build_tool_change_message_handler(
    upstream_id: str,
    on_tool_list_changed: OnToolListChanged | None,
    on_resource_list_changed: OnResourceListChanged | None = None,
    on_prompt_list_changed: OnPromptListChanged | None = None,
) -> MessageHandlerFnT:
    """Return a ``ClientSession`` message_handler that forwards
    ``notifications/{tools,resources,prompts}/list_changed`` to the
    matching callback.

    All other incoming messages are ignored (matching the SDK default).
    Exceptions raised by a callback are logged, never propagated, so a
    failing notifier cannot tear down the upstream session.
    """

    async def _handler(
        message: RequestResponder[mcp_types.ServerRequest, mcp_types.ClientResult]
        | mcp_types.ServerNotification
        | Exception,
    ) -> None:
        if not isinstance(message, mcp_types.ServerNotification):
            return
        root = message.root
        if isinstance(root, mcp_types.ToolListChangedNotification):
            if on_tool_list_changed is None:
                return
            try:
                on_tool_list_changed()
            except Exception:
                logger.exception(
                    "upstream.notification.tool_list_changed.callback_failed",
                    upstream_id=upstream_id,
                )
        elif isinstance(root, mcp_types.ResourceListChangedNotification):
            if on_resource_list_changed is None:
                return
            try:
                on_resource_list_changed()
            except Exception:
                logger.exception(
                    "upstream.notification.resource_list_changed.callback_failed",
                    upstream_id=upstream_id,
                )
        elif isinstance(root, mcp_types.PromptListChangedNotification):
            if on_prompt_list_changed is None:
                return
            try:
                on_prompt_list_changed()
            except Exception:
                logger.exception(
                    "upstream.notification.prompt_list_changed.callback_failed",
                    upstream_id=upstream_id,
                )

    return _handler
