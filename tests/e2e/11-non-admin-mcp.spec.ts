import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import { makeMcpClient, mintMcpToken, apiLoginAs, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN = "admin@example.com";

// Dedicated role + user so these tests can mutate access config freely
// without racing the other specs that also touch the seeded ``user``
// role. Both are created in ``beforeAll`` and removed in ``afterAll``.
const NON_ADMIN_ROLE = "e2e-nonadmin";
const NON_ADMIN_EMAIL = "e2e-nonadmin@example.com";

async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN);
  return request;
}

async function resetRoleAccess(api: APIRequestContext) {
  // Blank slate: test-tools enabled, allow-all tools, no overrides.
  await api.put(
    `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/mcps/test-tools`,
    { data: { enabled: true } }
  );
  await api.put(
    `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tool-fallback-enabled`,
    { data: { fallback_enabled: true } }
  );
  for (const tool of ["echo", "add", "greet"]) {
    await api.delete(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tools/${tool}`
    );
  }
}

test.describe("Non-admin gateway access", () => {
  let client: Client | null = null;

  test.beforeAll(async ({ request }) => {
    const api = await adminApi(request);
    // Fresh each run — drop any leftovers from a previous aborted run.
    await api.delete(`${BACKEND}/api/admin/users/${NON_ADMIN_EMAIL}`);
    await api.delete(`${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}`);

    const roleResp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: NON_ADMIN_ROLE, copy_from: "user" },
    });
    expect([200, 201]).toContain(roleResp.status());

    // copy_from inherits user's current mcp_access. Normalize so later
    // tests always start from a known state regardless of what the
    // ``user`` role happened to look like at snapshot time.
    await resetRoleAccess(api);

    const userResp = await api.post(`${BACKEND}/api/admin/users`, {
      data: { email: NON_ADMIN_EMAIL, role: NON_ADMIN_ROLE },
    });
    expect([200, 201]).toContain(userResp.status());
  });

  test.afterAll(async ({ request }) => {
    const api = await adminApi(request);
    await api.delete(`${BACKEND}/api/admin/users/${NON_ADMIN_EMAIL}`);
    await api.delete(`${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}`);
  });

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("non-admin lists the tools their role allows", async ({ request }) => {
    const api = await adminApi(request);
    await resetRoleAccess(api);

    const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
    client = await makeMcpClient(token, ORG, "mcp");

    const result = await client.listTools();
    const names = result.tools.map((t) => t.name);
    // Multi-org gateway prefixes every tool with the org slug.
    expect(names).toContain(`${ORG}__test-tools__echo`);
    expect(names).toContain(`${ORG}__test-tools__add`);
    expect(names).toContain(`${ORG}__test-tools__greet`);
  });

  test("non-admin sees no tools after MCP access is revoked", async ({
    request,
  }) => {
    const api = await adminApi(request);
    await resetRoleAccess(api);
    await api.put(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/mcps/test-tools`,
      { data: { enabled: false } }
    );

    try {
      const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
      client = await makeMcpClient(token, ORG, "mcp");

      const result = await client.listTools();
      const names = result.tools.map((t) => t.name);
      expect(names.some((n) => n.indexOf("test-tools__") !== -1)).toBe(false);
    } finally {
      await resetRoleAccess(api);
    }
  });

  test("non-admin can call an allowed tool", async ({ request }) => {
    const api = await adminApi(request);
    await resetRoleAccess(api);

    const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
    client = await makeMcpClient(token, ORG, "mcp");

    const resp = await client.callTool({
      name: `${ORG}__test-tools__echo`,
      arguments: { message: "hi from user" },
    });
    expect(JSON.stringify(resp.content)).toContain("hi from user");
  });

  test("argument constraint denial surfaces as an Access denied response", async ({
    request,
  }) => {
    const api = await adminApi(request);
    await resetRoleAccess(api);
    await api.put(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "evil", mode: "forbid" } }
    );

    try {
      const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
      client = await makeMcpClient(token, ORG, "mcp");

      // Matches the forbid pattern — policy must deny before the call
      // ever reaches the upstream.
      const denied = await client.callTool({
        name: `${ORG}__test-tools__echo`,
        arguments: { message: "evil plans" },
      });
      expect(JSON.stringify(denied.content)).toContain("Access denied");

      // A message that doesn't match the pattern still goes through,
      // proving the constraint was the reason for the denial above.
      const ok = await client.callTool({
        name: `${ORG}__test-tools__echo`,
        arguments: { message: "nice plans" },
      });
      expect(JSON.stringify(ok.content)).toContain("nice plans");
    } finally {
      await api.delete(
        `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tools/echo/constraints/message`
      );
    }
  });

  test("policy deny for a specific tool surfaces as an Access denied response", async ({
    request,
  }) => {
    const api = await adminApi(request);
    await resetRoleAccess(api);
    // Flip add to deny for the role while leaving echo allowed.
    await api.put(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: null } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tools/echo`,
      { data: { enabled: true } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/${NON_ADMIN_ROLE}/upstreams/test-tools/tools/add`,
      { data: { enabled: false } }
    );

    try {
      const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
      client = await makeMcpClient(token, ORG, "mcp");

      const resp = await client.callTool({
        name: `${ORG}__test-tools__add`,
        arguments: { a: 1, b: 2 },
      });
      const text = JSON.stringify(resp.content);
      expect(text).toContain("Access denied");
      expect(text).not.toContain("\"3\"");
    } finally {
      await resetRoleAccess(api);
    }
  });
});

test.describe("Non-admin admin MCP access", () => {
  test.beforeAll(async ({ request }) => {
    // Make sure the non-admin user exists even if this describe runs
    // before the gateway describe (Playwright does not guarantee order
    // across describes in the same file, though in practice they do).
    const api = await adminApi(request);
    await api.post(`${BACKEND}/api/admin/users`, {
      data: { email: NON_ADMIN_EMAIL, role: "user" },
    });
  });

  test("non-admin cannot connect to the admin MCP", async ({ request }) => {
    const token = await mintMcpToken(request, NON_ADMIN_EMAIL, ORG);
    // The MCP SDK raises when the initialize POST fails; if it ever
    // returns a Client the first listTools must fail. Either is a
    // pass — admins only.
    let rejected = false;
    try {
      const client = await makeMcpClient(token, ORG, "admin-mcp");
      await client.listTools();
      await client.close().catch(() => {});
    } catch {
      rejected = true;
    }
    expect(rejected).toBe(true);
  });
});
