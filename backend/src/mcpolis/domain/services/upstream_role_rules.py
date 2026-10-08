"""Drop every role-level rule attached to one upstream MCP.

Shared by both config repositories (file and Mongo): removing an
upstream calls it, and so does adding one, so a new upstream never
inherits rules left under the same id.
"""
from __future__ import annotations

from mcpolis.domain.model.settings import SettingsConfig
from mcpolis.domain.services.tool_registry import SEPARATOR


def _constraint_key_upstream(key: str) -> str | None:
    """Argument-check keys are ``<upstream>__<tool>``, the gateway's tool
    name, and the gateway splits that name at its FIRST ``__``
    (``ToolRegistry.resolve_tool``). So a key belongs to the text before
    its first ``__``, whatever the tool name holds. A key without ``__``
    is a bare tool name that applies to every upstream."""
    upstream_id, separator, _ = key.partition(SEPARATOR)
    return upstream_id if separator else None


def remove_upstream_role_rules(config: SettingsConfig, upstream_id: str) -> bool:
    """Remove *upstream_id*'s access entry, tool access overrides and
    argument checks from every role, in place. Returns whether anything
    was removed. An id that itself contains ``__`` owns no argument-check
    key, since the gateway can't route to it either."""
    changed = False
    for role in config.roles.values():
        settings = role.settings
        if settings.mcp_access.mcps.pop(upstream_id, None) is not None:
            changed = True
        if settings.tool_access.pop(upstream_id, None) is not None:
            changed = True
        for key in [
            k for k in settings.argument_constraints
            if _constraint_key_upstream(k) == upstream_id
        ]:
            del settings.argument_constraints[key]
            changed = True
    return changed
