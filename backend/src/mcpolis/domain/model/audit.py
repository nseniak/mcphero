from __future__ import annotations

import re

from pydantic import BaseModel


class AuditEntry(BaseModel):
    """One audited action.

    Deliberately does NOT carry the tool arguments: arguments may contain
    secrets (API keys, tokens passed as parameters), and the audit log
    records *what* was called and *whether it was allowed*, not the
    argument values.
    """
    timestamp: str
    action: str = "tool_call"
    # Keeps the literal in sync with ``DEFAULT_ORG_ID`` in
    # ``mcpolis.domain.ports`` — avoids a circular import from the model
    # layer into the ports package.
    org_id: str = "default"
    user_id: str
    upstream_id: str
    auth_mode: str = ""
    auth_identity: str = ""
    tool: str = ""
    policy_decision: str = ""
    policy_rule: str | None = None
    response_status: str = ""
    latency_ms: float | None = None
    session_id: str | None = None
    outcome: str | None = None
    error_message: str | None = None
    client_type: str | None = None


# Patterns: (regex, friendly name)
_CLIENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Claude(?:\s+Code|Code)", re.IGNORECASE), "Claude Code"),
    (re.compile(r"ClaudeDesktop", re.IGNORECASE), "Claude Desktop"),
    (re.compile(r"Cursor", re.IGNORECASE), "Cursor"),
    (re.compile(r"Windsurf", re.IGNORECASE), "Windsurf"),
    (re.compile(r"VS\s*Code|Visual\s*Studio\s*Code", re.IGNORECASE), "VS Code"),
    (re.compile(r"JetBrains|IntelliJ|PyCharm|WebStorm|GoLand|Rider|CLion|RubyMine|PhpStorm|DataGrip", re.IGNORECASE), "JetBrains"),
    (re.compile(r"Zed", re.IGNORECASE), "Zed"),
    (re.compile(r"Cline", re.IGNORECASE), "Cline"),
    (re.compile(r"Continue", re.IGNORECASE), "Continue"),
    (re.compile(r"Roo[\s-]?Code", re.IGNORECASE), "Roo Code"),
    (re.compile(r"MCP Inspector", re.IGNORECASE), "MCP Inspector"),
]


def parse_client_type(user_agent: str | None) -> str:
    """Extract a friendly client type name from a User-Agent string."""
    if not user_agent:
        return "Unknown"
    for pattern, name in _CLIENT_PATTERNS:
        if pattern.search(user_agent):
            return name
    return user_agent.split("/")[0][:40] if "/" in user_agent else "Unknown"
