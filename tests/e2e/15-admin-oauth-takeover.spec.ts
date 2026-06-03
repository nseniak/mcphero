/**
 * Two-admin admin_oauth take-over flow against a real OAuth-demanding
 * upstream.
 *
 * The seed in run-e2e-tests.sh registers ``oauth-tools`` with
 * auth_mode=admin_oauth pointing at tests/e2e/oauth_test_mcp_server.py
 * (a fake provider that auto-approves /authorize and validates
 * Bearer tokens on /mcp/). The two admins are admin@example.com
 * (admin A) and admin2@example.com (admin B).
 *
 * Behaviours covered (none of which were previously e2e-tested):
 *   - admin A connects -> slot_owner resolves to A
 *   - admin B sees A's badge from a different session
 *   - admin B's connect attempt 409s with "A is already connected"
 *   - admin B's disconnect releases the slot
 *   - admin B connects -> owner flips to B
 *   - tools forwarded through the gateway carry B's upstream token
 *     (the fake upstream echoes ``as=<email>`` so the test can read
 *     the slot owner indirectly through the data plane)
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import { apiLoginAs, makeMcpClient, mintMcpToken, OAUTH_TEST_MCP_URL, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN_A = "admin@example.com";
const ADMIN_B = "admin2@example.com";
const UPSTREAM = "oauth-tools";

async function adminApi(request: APIRequestContext, email: string) {
  await apiLoginAs(request, email);
  return request;
}

async function getUpstream(api: APIRequestContext, id: string) {
  const resp = await api.get(`${BACKEND}/api/admin/upstreams/${id}`);
  expect(resp.status()).toBe(200);
  return resp.json();
}

async function listUpstreams(api: APIRequestContext) {
  const resp = await api.get(`${BACKEND}/api/admin/upstreams`);
  expect(resp.status()).toBe(200);
  return resp.json();
}

async function slotOwner(api: APIRequestContext): Promise<string | null> {
  const detail = await getUpstream(api, UPSTREAM);
  return (detail.slot_owner as string | null) ?? null;
}

/**
 * Walk MCPolis's connect flow end-to-end against the fake OAuth
 * upstream. Returns once the connect call reports ``connected=true``.
 *
 * The shape is:
 *   1) POST /api/admin/upstreams/<id>/connect
 *      -> { authorization_url } (the admin's browser would open this)
 *   2) GET <authorization_url>&email=<who> (no redirect follow)
 *      -> 302 to /api/oauth/upstream/callback?code=...&state=...
 *   3) GET <callback_url> (no redirect follow)
 *      -> 200 (HTML success page)
 *   4) Re-fetch the upstream detail; slot_owner == <who>.
 */
async function completeAdminOauthConnect(
  api: APIRequestContext,
  loggedInAs: string
) {
  const connectResp = await api.post(
    `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
  );
  expect(connectResp.status()).toBe(200);
  const body = await connectResp.json();
  if (body.connected) {
    // The slot was already populated for this admin (silent refresh).
    return;
  }
  expect(body.authorization_url).toBeTruthy();

  // The fake provider lets us pass ?email=<who> so the eventual
  // token's identity matches the admin doing the connecting.
  const authorizeUrl = new URL(body.authorization_url);
  authorizeUrl.searchParams.set("email", loggedInAs);

  const authorizeResp = await api.get(authorizeUrl.toString(), {
    maxRedirects: 0,
  });
  expect(authorizeResp.status()).toBe(302);
  const callbackLoc = authorizeResp.headers()["location"];
  expect(callbackLoc).toContain("/api/oauth/upstream/callback");

  const callbackResp = await api.get(callbackLoc, { maxRedirects: 0 });
  expect(callbackResp.status()).toBe(200);

  // Give the gateway a moment to flush the token-store write +
  // tool re-discovery before the next assertion. The connect path
  // schedules these as fire-and-forget side effects of the
  // callback; tests that immediately ask for the owner sometimes
  // race the write.
  await new Promise((r) => setTimeout(r, 200));
}

test.describe("admin_oauth single-slot take-over", () => {
  test.beforeEach(async ({ request }) => {
    // Wipe both sides of the fake-vs-MCPolis state so a previous
    // spec's queued email / TTL knob / connected slot can't leak
    // into this test.
    await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
    await adminApi(request, ADMIN_A);
    await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
    );
  });

  test("admin A connect -> owner becomes A; admin B sees A's badge", async ({
    request,
  }) => {
    await adminApi(request, ADMIN_A);
    expect(await slotOwner(request)).toBeNull();

    await completeAdminOauthConnect(request, ADMIN_A);
    expect(await slotOwner(request)).toBe(ADMIN_A);

    // Switch to admin B (different cookie). The owner field is
    // shared org state, not per-session — both admins must see the
    // same value.
    await adminApi(request, ADMIN_B);
    expect(await slotOwner(request)).toBe(ADMIN_A);

    const summary = await listUpstreams(request);
    const row = summary.find((u: { id: string }) => u.id === UPSTREAM);
    expect(row.slot_owner).toBe(ADMIN_A);
    expect(row.ready).toBe(true);

    // ``/api/user/mcps`` is the /my-tools surface. Under the unified
    // readiness model, ``ready`` mirrors the admin tab's view —
    // admin_oauth ⇒ Ready iff at least one admin has authenticated.
    // Assert from BOTH admins' perspective: the row is Ready for
    // everyone whenever an admin holds the slot.
    const myToolsB = await request.get(`${BACKEND}/api/user/mcps`);
    const myRowB = (await myToolsB.json()).find(
      (m: { id: string }) => m.id === UPSTREAM,
    );
    expect(myRowB.ready).toBe(true);
    expect(myRowB.user_connection_status).toBe("connected");

    await adminApi(request, ADMIN_A);
    const myToolsA = await request.get(`${BACKEND}/api/user/mcps`);
    const myRowA = (await myToolsA.json()).find(
      (m: { id: string }) => m.id === UPSTREAM,
    );
    expect(myRowA.ready).toBe(true);
  });

  test("admin B's connect 409s while A owns the slot", async ({ request }) => {
    await adminApi(request, ADMIN_A);
    await completeAdminOauthConnect(request, ADMIN_A);

    await adminApi(request, ADMIN_B);
    const conflict = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
    );
    expect(conflict.status()).toBe(409);
    const detail = (await conflict.json()).detail;
    expect(detail).toContain(ADMIN_A);
    expect(detail.toLowerCase()).toContain("disconnect");
  });

  test("admin B's disconnect releases the slot, then B can claim it", async ({
    request,
  }) => {
    await adminApi(request, ADMIN_A);
    await completeAdminOauthConnect(request, ADMIN_A);

    await adminApi(request, ADMIN_B);
    const disconnect = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
    );
    expect(disconnect.status()).toBe(200);
    expect(await slotOwner(request)).toBeNull();

    await completeAdminOauthConnect(request, ADMIN_B);
    expect(await slotOwner(request)).toBe(ADMIN_B);

    // Original admin sees the new owner from their own session.
    await adminApi(request, ADMIN_A);
    expect(await slotOwner(request)).toBe(ADMIN_B);
  });

  test("gateway forwards the slot owner's upstream token to tools", async ({
    request,
  }) => {
    // Admin A claims the slot; tools route through A's token.
    await adminApi(request, ADMIN_A);
    await completeAdminOauthConnect(request, ADMIN_A);

    // Hit the upstream tool through the MCP gateway as ADMIN_B.
    // For admin_oauth the gateway picks any admin's valid token —
    // here that's A's, so the upstream sees ``as=ADMIN_A``.
    const tokenB = await mintMcpToken(request, ADMIN_B, ORG);
    let mcp = await makeMcpClient(tokenB, ORG, "mcp");
    try {
      const result = await mcp.callTool({
        name: `${ORG}__${UPSTREAM}__secret_echo`,
        arguments: { message: "hello" },
      });
      const content = result.content as Array<{ type: string; text?: string }>;
      const text = content.find((c) => c.type === "text")?.text ?? "";
      expect(text).toContain("hello");
      expect(text).toContain(`as=${ADMIN_A}`);
    } finally {
      await mcp.close();
    }

    // Admin B takes over. The gateway must now forward B's token —
    // the tool's ``as=`` field is the cleanest read on which token
    // is in flight (it's the upstream's view, not MCPolis's).
    await adminApi(request, ADMIN_B);
    await request.post(`${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`);
    await completeAdminOauthConnect(request, ADMIN_B);

    mcp = await makeMcpClient(tokenB, ORG, "mcp");
    try {
      const result = await mcp.callTool({
        name: `${ORG}__${UPSTREAM}__secret_echo`,
        arguments: { message: "hello" },
      });
      const content = result.content as Array<{ type: string; text?: string }>;
      const text = content.find((c) => c.type === "text")?.text ?? "";
      expect(text).toContain(`as=${ADMIN_B}`);
    } finally {
      await mcp.close();
    }
  });
});
