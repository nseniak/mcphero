/**
 * Both admin MCP endpoints answer a signed-in client at the full stack.
 *
 * (a) ``/admin-mcp/system`` is the superadmin MCP. The backend mounts it
 *     in cloud mode only, when ``MCPOLIS_SUPERADMIN_EMAILS`` is set (the
 *     e2e stack sets both). The backend's startup never started its
 *     session manager, so every request got 500 ("Task group is not
 *     initialized"). The unit-level check,
 *     backend/tests/unit/test_mcp_endpoints_start_at_boot.py, needs a
 *     reachable Mongo and skips without one; this spec is the full-stack
 *     check.
 * (b) Production nginx forwards the public Host (``proxy_set_header Host
 *     $host``), and the MCP SDK's host check answered 421 to any Host but
 *     localhost. e2e calls the backend on 127.0.0.1, so it never sent a
 *     public Host; these probes set one explicitly.
 *
 * Read-only: each probe mints its own bearer and only sends
 * ``initialize``, so co-located specs are unaffected.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import { mintMcpToken, BACKEND_URL as BACKEND } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const SUPERADMIN = "superadmin@example.com";

const ENDPOINTS = [
  {
    label: "admin",
    path: `admin-mcp/${ORG}/`,
    email: ADMIN,
    serverName: '"name":"MCP Hero Admin',
  },
  {
    label: "superadmin",
    path: "admin-mcp/system/",
    email: SUPERADMIN,
    serverName: '"name":"MCP Hero Superadmin"',
  },
];

async function postInitialize(
  request: APIRequestContext,
  path: string,
  token: string,
  siteHeaders: Record<string, string>,
) {
  return request.post(`${BACKEND}/${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...siteHeaders,
    },
    data: {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "e2e-probe", version: "0" },
      },
    },
    maxRedirects: 0,
  });
}

for (const endpoint of ENDPOINTS) {
  test(`${endpoint.label} MCP answers initialize after boot`, async ({
    request,
  }) => {
    const token = await mintMcpToken(request, endpoint.email, ORG);
    const resp = await postInitialize(request, endpoint.path, token, {});
    const body = await resp.text();
    expect(resp.status(), body).toBe(200);
    expect(body).toContain(endpoint.serverName);
  });

  test(`${endpoint.label} MCP answers initialize for the public Host`, async ({
    request,
  }) => {
    const token = await mintMcpToken(request, endpoint.email, ORG);
    const resp = await postInitialize(request, endpoint.path, token, {
      Host: "mcphero.io",
    });
    const body = await resp.text();
    expect(resp.status(), body).toBe(200);
    expect(body).toContain(endpoint.serverName);
  });
}
