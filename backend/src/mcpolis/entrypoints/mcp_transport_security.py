"""Host-check setting shared by every MCP endpoint the backend serves.

The MCP SDK's DNS-rebinding protection refuses a request whose ``Host``
header is not localhost (421 "Invalid Host header") or whose browser
``Origin`` is not localhost (403). It is meant for servers that trust
anyone able to reach them on the local machine. FastMCP turns it on by
itself whenever it is built with its default host (``127.0.0.1``), which
made ``/admin-mcp/<slug>`` and ``/admin-mcp/system`` refuse every real
client: production nginx forwards the public Host. Dev and e2e never
saw it, because the Vite proxy rewrites Host to the backend's loopback
address and e2e calls 127.0.0.1.

It stays off for every endpoint, decided here once:

- The gateway and both admin endpoints check a bearer token before a
  request reaches the MCP server, and browsers never attach one on
  their own. In production a bearer takes a real Google sign-in. In
  dev, the test-mode route that mints bearers refuses web pages
  (``test-mcp-token`` in ``dashboard_auth.py``), because CORS lets any
  site read its answer. A DNS-rebinding page aimed at a local dev-stub
  instance can still sign in, but the dashboard API, which never had a
  host check, already gives it the same powers.
- The demo endpoint is an unauthenticated fixture with harmless tools,
  reached through tunnels and proxies whose Host varies.

Pass it explicitly even where the SDK default is already off (the
gateway's bare ``StreamableHTTPSessionManager``), so a new SDK default
cannot switch the check on unnoticed.
"""
from __future__ import annotations

from mcp.server.transport_security import TransportSecuritySettings


def mcp_transport_security() -> TransportSecuritySettings:
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)
