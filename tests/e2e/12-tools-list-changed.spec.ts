import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { Tool } from "@modelcontextprotocol/sdk/types.js";

import { makeMcpClient, mintMcpToken, apiLoginAs, BACKEND_URL as BACKEND, TEST_MCP_URL } from "./helpers";
const ORG = "acme-corp";
const ADMIN = "admin@example.com";

// Dedicated upstream for this spec so the disruptive ``reconnect``
// call below can't tear down a session another test (e.g.
// ``10-mcp-tool-invocation``) is mid-call against. Same upstream
// implementation as the seeded ``test-tools`` — different id is the
// only difference. Created in ``beforeAll``, removed in ``afterAll``.
const UPSTREAM_ID = "test-tools-12";

/**
 * End-to-end coverage for ``notifications/tools/list_changed``.
 *
 * The gateway Server must (a) declare ``tools.listChanged: true`` in
 * its initialize response, and (b) actually push a notification when
 * PolicyNotifier fires. Both pieces have regressed in the past; this
 * spec nails both down with a real MCP streamable-HTTP client.
 *
 * The E2E harness sets ``MCPOLIS_POLICY_NOTIFIER_DEBOUNCE_SECONDS=0.1``
 * so the debounce window doesn't dominate the test runtime.
 */

async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN);
  return request;
}

async function waitUntil(
  predicate: () => boolean,
  timeoutMs: number,
  pollMs = 50
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return predicate();
}

test.describe("tools/list_changed notifications", () => {
  let client: Client | null = null;

  test.beforeAll(async ({ request }) => {
    const api = await adminApi(request);
    // Drop any leftover upstream from a previous aborted run.
    await api.delete(`${BACKEND}/api/admin/upstreams/${UPSTREAM_ID}`);

    // Display name deliberately avoids the substring "Test Tools" so
    // it doesn't collide with other specs' ``getByText("Test Tools")``
    // locators when this spec runs in parallel with them.
    const createResp = await api.post(`${BACKEND}/api/admin/upstreams`, {
      data: {
        id: UPSTREAM_ID,
        display_name: "List-Changed Spec Upstream",
        url: `${TEST_MCP_URL}/mcp`,
        auth_mode: "service_account",
      },
    });
    expect([200, 201]).toContain(createResp.status());

    const reconnectResp = await api.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`
    );
    expect(reconnectResp.status()).toBe(200);
  });

  test.afterAll(async ({ request }) => {
    const api = await adminApi(request);
    await api.delete(`${BACKEND}/api/admin/upstreams/${UPSTREAM_ID}`);
  });

  test.afterEach(async () => {
    if (client) {
      await client.close().catch(() => {});
      client = null;
    }
  });

  test("gateway advertises tools.listChanged=true", async ({ request }) => {
    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "mcp");
    const caps = client.getServerCapabilities();
    expect(caps?.tools?.listChanged).toBe(true);
  });

  test("admin role mutation pushes tools/list_changed to connected sessions", async ({
    request,
  }) => {
    let fired = 0;
    let latestTools: Tool[] | null = null;

    const token = await mintMcpToken(request, ADMIN, ORG);
    client = await makeMcpClient(token, ORG, "mcp", {
      listChanged: {
        tools: {
          onChanged: (_error, tools) => {
            fired += 1;
            latestTools = tools ?? null;
          },
          // Disable client-side debounce so the handler fires as soon
          // as the server pushes — otherwise we'd stack two waits.
          debounceMs: 0,
        },
      },
    });

    // Warm up the session (list once so the SDK has a tool list to diff
    // against if it does a diff — either way, listChanged fires on the
    // raw notification).
    await client.listTools();

    // Trigger a broadcast notification. admin reconnect fires
    // _notify_policy_change() (broadcast) which reaches every session
    // including this one, no matter which role the caller holds.
    // The reconnect targets this spec's *own* upstream so other specs
    // running in parallel don't see their tool calls disrupted by it.
    const api = await adminApi(request);
    const resp = await api.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`
    );
    expect(resp.status()).toBe(200);

    // Debounce is 0.1s in the E2E harness — 5s is plenty of headroom.
    const ok = await waitUntil(() => fired > 0, 5000);
    expect(ok).toBe(true);
    expect(fired).toBeGreaterThan(0);
    // autoRefresh defaults to true, so the SDK re-listed for us.
    expect(latestTools).not.toBeNull();
    expect(latestTools!.map((t) => t.name)).toContain(
      `${ORG}__${UPSTREAM_ID}__echo`
    );
  });
});
