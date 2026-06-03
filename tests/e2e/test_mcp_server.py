"""Standalone runner for the bundled MCP Hero demo.

The actual server (tools, resources, prompts, widgets) lives under
``backend/src/mcpolis/dev/demo_mcp_server.py`` so it can be mounted
into the main backend (Part B). This file is a thin shim retained
so the e2e harness keeps working: ``python tests/e2e/test_mcp_server.py``
spins the same demo on ``127.0.0.1:9999`` against
``MCPOLIS_DEMO_PUBLIC_URL`` (default ``http://127.0.0.1:9999``) for
widgets that phone home.

Run:  python tests/e2e/test_mcp_server.py
Listens on http://localhost:9999/mcp (StreamableHTTP).
"""
from __future__ import annotations

import sys
from pathlib import Path

# When invoked as ``python tests/e2e/test_mcp_server.py`` (e.g. by the
# Playwright harness) the package isn't on ``sys.path``. Push the
# backend's src/ onto the path so the import works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from mcpolis.dev.demo_mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
