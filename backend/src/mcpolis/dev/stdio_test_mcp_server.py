# /// script
# dependencies = ["mcp"]
# ///
# pyright: reportUnusedFunction=false
"""Controllable stdio MCP fixture for the integration / E2E suites.

Three MCP tools and two CLI flags. Designed to be runnable directly
inside an E2B sandbox via ``uv run <https-url>`` (PEP 723 inline
deps) — the sandbox launcher fetches the script over the dev backend
tunnel, ``uv`` resolves dependencies in an ephemeral environment,
and the script speaks JSON-RPC over stdio just like any other stdio
MCP.

Tools:

- ``read_file(path)`` — return the contents of an absolute path.
  Used by the Sandbox-files materialization integration test to
  assert the launcher dropped the file at the expected location
  with the expected body.
- ``read_env(name)`` — return the value of an env var. Used to
  assert ``${...}`` Variable substitution actually resolved into
  the launched process's environment.
- ``list_dir(path)`` — directory listing. Used to assert the
  pre-exec hook created parent directories.

CLI flags (read at process start):

- ``--hang-at-init`` — stub out the JSON-RPC ``initialize`` handler
  so the process never responds. Drives workstream A's
  :class:`StdioInitTimeout` test against a controllable fixture.
- ``--exit-during-init <code>`` — ``sys.exit(code)`` before the
  initialize handshake completes. Smoke test for the existing
  :class:`SubprocessExitedDuringInit` path.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def build_server() -> FastMCP:
    """Construct the FastMCP instance.

    Module-scoped builder so unit tests can import + introspect the
    server's tool registry without going through stdio.
    """
    server = FastMCP("stdio-test-mcp")

    @server.tool()
    def read_file(path: str) -> str:
        """Return the contents of ``path`` (must be absolute)."""
        return Path(path).read_text(encoding="utf-8")

    @server.tool()
    def read_env(name: str) -> str:
        """Return the value of the named env var, or empty string."""
        return os.environ.get(name, "")

    @server.tool()
    def list_dir(path: str) -> list[str]:
        """Return a sorted directory listing for ``path``."""
        p = Path(path)
        if not p.is_dir():
            return []
        return sorted(entry.name for entry in p.iterdir())

    return server


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Controllable stdio MCP fixture",
    )
    parser.add_argument(
        "--hang-at-init",
        action="store_true",
        help=(
            "Never respond to the JSON-RPC initialize handshake — the "
            "process stays alive but the client times out."
        ),
    )
    parser.add_argument(
        "--exit-during-init",
        type=int,
        default=None,
        metavar="CODE",
        help=(
            "sys.exit(CODE) before completing the JSON-RPC initialize "
            "handshake."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    if args.exit_during_init is not None:
        # Don't even read stdin — the client races initialize against
        # exit, so dying immediately is the most direct test of the
        # SubprocessExitedDuringInit path.
        sys.exit(args.exit_during_init)
    if args.hang_at_init:
        # Block forever without responding. The client should observe
        # ``StdioInitTimeout`` after the configured timeout. ``read``
        # on stdin parks the process in a non-busy wait — better than
        # ``while True: pass``.
        sys.stdin.read()
        return
    server = build_server()
    # FastMCP.run() defaults to stdio transport; ``transport="stdio"``
    # makes that explicit so a future SDK default flip can't silently
    # break the fixture.
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
