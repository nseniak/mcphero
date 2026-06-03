import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import { makeMcpClient, mintMcpToken, apiLoginAs, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN = "admin@example.com";

/**
 * End-to-end tool invocation through the gateway MCP and admin MCP
 * as an admin user. Uses a real MCP streamable-HTTP client so the
 * test exercises session init, tools/list, tools/call, and the
 * audit trail all the way through.
 */

async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN);
  return request;
}

test.describe("Gateway MCP tool invocation (admin)", () => {
  let client: Client | null = null;

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("tools/list returns the seeded upstream's tools, prefixed with the org slug", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "mcp");

    const result = await client.listTools();
    const names = result.tools.map((t) => t.name);
    // Cloud user gateway aggregates per-org; tool names carry the
    // ``{slug}__`` prefix so clients can disambiguate same-named
    // upstreams across orgs.
    expect(names).toContain(`${ORG}__test-tools__echo`);
    expect(names).toContain(`${ORG}__test-tools__add`);
    expect(names).toContain(`${ORG}__test-tools__greet`);
  });

  test("tools/call with a bogus bare name is rejected", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "mcp");

    // The multi-org gateway accepts bare names when they uniquely
    // resolve to one upstream (this is the MCP-Apps widget callback
    // path — widgets call ``app.callServerTool({name: "foo"})`` with
    // no idea they're being proxied). When the bare name doesn't
    // match any known tool, the gateway returns an "Unknown tool"
    // error rather than touching any upstream.
    const resp = await client.callTool({
      name: "bogus-no-separator",
      arguments: {},
    });
    expect(JSON.stringify(resp.content).toLowerCase()).toContain(
      "unknown tool"
    );
  });

  test("tools/call echoes and adds, and the call lands in the audit log", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "mcp");

    const echoResp = await client.callTool({
      name: `${ORG}__test-tools__echo`,
      arguments: { message: "hello" },
    });
    const echoText = JSON.stringify(echoResp.content);
    expect(echoText).toContain("hello");

    const addResp = await client.callTool({
      name: `${ORG}__test-tools__add`,
      arguments: { a: 2, b: 3 },
    });
    expect(JSON.stringify(addResp.content)).toContain("5");

    // Audit log picks up the echo call — the admin UI queries this
    // same endpoint, so if it works here it works there too. The
    // audit entry stores the inner ``{upstream}__{tool}`` name (the
    // org dimension is already on the entry's ``org_id`` field).
    const api = await adminApi(request);
    const auditResp = await api.get(
      `${BACKEND}/api/admin/audit?tool=test-tools__echo&user_id=${ADMIN}`
    );
    expect(auditResp.status()).toBe(200);
    const audit = await auditResp.json();
    expect(audit.count).toBeGreaterThan(0);
    const entry = audit.entries.find(
      (e: { tool?: string }) => e.tool === "test-tools__echo"
    );
    expect(entry).toBeDefined();
    expect(entry.user_id).toBe(ADMIN);
  });
});

test.describe("Admin MCP tool invocation (admin)", () => {
  let client: Client | null = null;

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("admin MCP tools/list includes list_upstreams", async ({ request }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "admin-mcp");

    const result = await client.listTools();
    const names = result.tools.map((t) => t.name);
    expect(names).toContain("list_upstreams");
  });

  test("admin MCP list_upstreams returns the test-tools upstream", async ({
    request,
  }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "admin-mcp");

    const resp = await client.callTool({
      name: "list_upstreams",
      arguments: {},
    });
    const text = JSON.stringify(resp.content);
    expect(text).toContain("test-tools");
  });
});

// Cross-org *token* isolation is no longer enforced at the URL level:
// gateway tokens are user-scoped, and the user gateway lives at the
// fixed ``/mcp`` URL. The cross-org check that matters now is the
// per-tool membership check (a slug-prefixed call to an org the user
// doesn't belong to is rejected by the gateway controller). That
// check has unit-test coverage in
// ``backend/tests/unit/test_multi_org_gateway.py`` —
// ``test_call_tool_rejects_org_slug_user_is_not_a_member_of``.
