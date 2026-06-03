"""Unit tests for the controllable stdio MCP test fixture.

The fixture itself runs over stdio in real tests; here we just import
the module + exercise the tool functions to make sure a bug in the
fixture surfaces locally rather than as a downstream integration
test failure.
"""
from __future__ import annotations

import pytest

from mcpolis.dev import stdio_test_mcp_server


def test_build_server_returns_fastmcp_instance() -> None:
    server = stdio_test_mcp_server.build_server()
    # FastMCP exposes a ``name`` attribute.
    assert getattr(server, "name", None) == "stdio-test-mcp"


def test_parse_hang_at_init_flag() -> None:
    args = stdio_test_mcp_server._parse_args(["--hang-at-init"])
    assert args.hang_at_init is True


def test_parse_exit_during_init_flag() -> None:
    args = stdio_test_mcp_server._parse_args(["--exit-during-init", "7"])
    assert args.exit_during_init == 7


def test_main_exit_during_init_short_circuits() -> None:
    # ``main`` should ``sys.exit`` before starting the FastMCP server.
    with pytest.raises(SystemExit) as ei:
        stdio_test_mcp_server.main(["--exit-during-init", "3"])
    assert ei.value.code == 3
