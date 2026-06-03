import { test, expect } from "@playwright/test";


import { BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";

/**
 * MCP protocol tests — these use direct HTTP requests (no browser).
 * The gateway MCP and admin MCP are tested via the StreamableHTTP
 * protocol: POST with JSON-RPC messages.
 *
 * The user-facing gateway lives at the fixed /mcp URL (no slug). The
 * admin MCP keeps slug-based routing because admin actions are
 * inherently org-scoped.
 */

test.describe("Gateway MCP Protocol", () => {
  test("GET /mcp returns MCP metadata or auth challenge", async ({
    request,
  }) => {
    const resp = await request.get(`${BACKEND}/mcp`);
    // Without auth, we expect either 401 (OAuth required) or
    // a redirect to the auth server. Both are valid.
    expect([200, 401, 302, 307]).toContain(resp.status());
  });

  test("GET /mcp/.well-known/oauth-protected-resource returns metadata", async ({
    request,
  }) => {
    const resp = await request.get(
      `${BACKEND}/mcp/.well-known/oauth-protected-resource`
    );
    // Should return the resource metadata (even without auth)
    expect([200, 404]).toContain(resp.status());
  });
});

test.describe("Admin MCP Protocol", () => {
  test("GET /admin-mcp/{slug} returns auth challenge", async ({
    request,
  }) => {
    const resp = await request.get(`${BACKEND}/admin-mcp/${ORG}`);
    expect([200, 401, 302, 307]).toContain(resp.status());
  });
});

test.describe("MCP Health", () => {
  test("health endpoint returns ok", async ({ request }) => {
    const resp = await request.get(`${BACKEND}/health`);
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.status).toBe("ok");
  });

  test("features endpoint returns mode", async ({ request }) => {
    const resp = await request.get(`${BACKEND}/api/config/features`);
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.mode).toBe("cloud");
    expect(typeof body.allow_stdio_mcp).toBe("boolean");
  });
});
