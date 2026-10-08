/**
 * Removing an upstream MCP drops its role-level rules in every role, and
 * adding one never inherits rules stored under the same id.
 *
 * docs/upstream-mcps.md promises that removing an MCP "drops any
 * per-tool overrides or argument checks attached to it (in roles)".
 * Before the fix the rules survived, so re-adding a server under the
 * same id silently brought back the old tool denials and argument
 * checks. The first test removes through the dashboard's trash icon,
 * the same click an admin makes.
 *
 * Both doors (dashboard, Admin MCP) go through one shared remove, which
 * also tells connected MCP clients to re-list their tools; the last
 * test pins that through the Admin MCP door.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { Tool } from "@modelcontextprotocol/sdk/types.js";

import {
  apiLoginAs,
  loginAs,
  makeMcpClient,
  mintMcpToken,
  BACKEND_URL as BACKEND,
  TEST_MCP_URL,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const UPSTREAM_ID = "role-rules-51";
const DISPLAY_NAME = "Role Rules 51";
// One id per test, so the tests never share state.
const STALE_UPSTREAM_ID = "role-rules-51b";
const LIVE_UPSTREAM_ID = "role-rules-51c";
const ROLES = ["admin", "user"];
// What every role gets for a newly added server.
const FRESH_TOOL_ACCESS = {
  fallback_enabled: true,
  category_defaults: { readOnly: true, destructive: true },
  tools: {},
};

type RoleAccess = {
  name: string;
  mcp_access: { mcps: Record<string, boolean> };
  tool_access: Record<
    string,
    {
      fallback_enabled: boolean | null;
      category_defaults: Record<string, boolean>;
      tools: Record<string, boolean>;
    }
  >;
  argument_constraints: Record<string, Record<string, { pattern: string }>>;
};

async function addUpstream(api: APIRequestContext, id: string): Promise<void> {
  const resp = await api.post(`${BACKEND}/api/admin/upstreams`, {
    data: {
      id,
      display_name: id === UPSTREAM_ID ? DISPLAY_NAME : id,
      url: "https://1.1.1.1/mcp",
      auth_mode: "service_account",
    },
  });
  expect([200, 201]).toContain(resp.status());
}

async function setRules(
  api: APIRequestContext, role: string, id: string,
): Promise<void> {
  const tool = `${BACKEND}/api/admin/roles/${role}/upstreams/${id}/tools/delete_repo`;
  expect((await api.put(tool, { data: { enabled: false } })).status()).toBe(200);
  const check = `${BACKEND}/api/admin/roles/${role}/upstreams/${id}/tools/create_issue/constraints/repo`;
  expect(
    (await api.put(check, { data: { pattern: "^acme/", mode: "allow" } })).status(),
  ).toBe(200);
}

async function rolesById(api: APIRequestContext): Promise<Map<string, RoleAccess>> {
  const resp = await api.get(`${BACKEND}/api/admin/roles/access`);
  expect(resp.status()).toBe(200);
  const roles = (await resp.json()) as RoleAccess[];
  return new Map(roles.map((r) => [r.name, r]));
}

async function removeAll(api: APIRequestContext): Promise<void> {
  await apiLoginAs(api, ADMIN);
  for (const id of [UPSTREAM_ID, STALE_UPSTREAM_ID, LIVE_UPSTREAM_ID]) {
    await api.delete(`${BACKEND}/api/admin/upstreams/${id}`);
  }
}

test.describe("Removing an upstream MCP drops its role rules", () => {
  test.beforeEach(async ({ request }) => removeAll(request));
  test.afterEach(async ({ request }) => removeAll(request));

  test("remove via the dashboard, re-add the same id: no old rules", async ({
    page,
    request,
  }) => {
    await apiLoginAs(request, ADMIN);
    await addUpstream(request, UPSTREAM_ID);
    for (const role of ROLES) await setRules(request, role, UPSTREAM_ID);

    let roles = await rolesById(request);
    for (const role of ROLES) {
      const r = roles.get(role)!;
      expect(r.tool_access[UPSTREAM_ID].tools).toEqual({ delete_repo: false });
      expect(r.argument_constraints[`${UPSTREAM_ID}__create_issue`]).toBeDefined();
    }

    // Remove through the Upstream MCPs page: trash icon, then confirm.
    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const row = page.locator("tr", { hasText: DISPLAY_NAME });
    await expect(row).toBeVisible({ timeout: 10_000 });
    const removed = page.waitForResponse(
      (r) =>
        r.request().method() === "DELETE" &&
        r.url().endsWith(`/api/admin/upstreams/${UPSTREAM_ID}`),
    );
    await row.locator("button").last().click();
    await page.getByRole("dialog").getByRole("button", { name: "Remove" }).click();
    expect((await removed).status()).toBe(200);
    await expect(row).toHaveCount(0);

    roles = await rolesById(request);
    for (const role of ROLES) {
      const r = roles.get(role)!;
      expect(r.mcp_access.mcps[UPSTREAM_ID]).toBeUndefined();
      expect(r.tool_access[UPSTREAM_ID]).toBeUndefined();
      expect(r.argument_constraints[`${UPSTREAM_ID}__create_issue`]).toBeUndefined();
    }

    // Re-add under the same id: it starts with fresh, rule-free defaults.
    await addUpstream(request, UPSTREAM_ID);
    roles = await rolesById(request);
    for (const role of ROLES) {
      const r = roles.get(role)!;
      expect(r.tool_access[UPSTREAM_ID]).toEqual(FRESH_TOOL_ACCESS);
      expect(r.argument_constraints[`${UPSTREAM_ID}__create_issue`]).toBeUndefined();
    }
  });

  test("rules written after the remove are not inherited by a re-add", async ({
    request,
  }) => {
    // A stale Access tab or a queued Admin MCP call can still write a
    // rule for a server that was just removed; nothing refuses it.
    await apiLoginAs(request, ADMIN);
    await addUpstream(request, STALE_UPSTREAM_ID);
    const removed = await request.delete(
      `${BACKEND}/api/admin/upstreams/${STALE_UPSTREAM_ID}`,
    );
    expect(removed.status()).toBe(200);
    await setRules(request, "user", STALE_UPSTREAM_ID);

    await addUpstream(request, STALE_UPSTREAM_ID);

    const user = (await rolesById(request)).get("user")!;
    expect(user.tool_access[STALE_UPSTREAM_ID]).toEqual(FRESH_TOOL_ACCESS);
    expect(
      user.argument_constraints[`${STALE_UPSTREAM_ID}__create_issue`],
    ).toBeUndefined();
  });

  test("an Admin MCP remove makes connected clients drop the server's tools", async ({
    request,
  }) => {
    await apiLoginAs(request, ADMIN);
    const created = await request.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: LIVE_UPSTREAM_ID,
        display_name: LIVE_UPSTREAM_ID,
        url: `${TEST_MCP_URL}/mcp`,
        auth_mode: "service_account",
      },
    });
    expect([200, 201]).toContain(created.status());
    const started = await request.post(
      `${BACKEND}/api/admin/upstreams/${LIVE_UPSTREAM_ID}/reconnect`,
    );
    expect(started.status()).toBe(200);

    const echo = `${ORG}__${LIVE_UPSTREAM_ID}__echo`;
    let notices = 0;
    let latest: Tool[] | null = null;
    const listedNames = (): string[] | null =>
      latest === null ? null : latest.map((t) => t.name);
    const token = await mintMcpToken(request, ADMIN, ORG);
    const clients: Client[] = [];
    try {
      const gateway = await makeMcpClient(token, ORG, "mcp", {
        listChanged: {
          tools: {
            onChanged: (_error, tools) => {
              notices += 1;
              latest = tools ?? null;
            },
            debounceMs: 0,
          },
        },
      });
      clients.push(gateway);
      const admin = await makeMcpClient(token, ORG, "admin-mcp");
      clients.push(admin);
      expect((await gateway.listTools()).tools.map((t) => t.name)).toContain(echo);
      // Let the notice the Start above triggered land first (the
      // harness debounces notices by 0.1 s), so the notice counted
      // below can only come from the remove.
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      const noticesBefore = notices;

      const removed = await admin.callTool({
        name: "remove_upstream",
        arguments: { mcp_id: LIVE_UPSTREAM_ID },
      });
      expect(JSON.stringify(removed.content)).toContain("removed");

      // The gateway pushes tools/list_changed; the client re-lists.
      await expect.poll(() => notices, { timeout: 5_000 }).toBeGreaterThan(
        noticesBefore,
      );
      expect(listedNames()).not.toContain(echo);
    } finally {
      for (const c of clients) await c.close().catch(() => {});
    }
  });
});
