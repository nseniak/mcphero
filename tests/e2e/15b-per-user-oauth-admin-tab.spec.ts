/**
 * Per-user OAuth take-over via the admin tab.
 *
 * Sibling spec to ``15-admin-oauth-takeover``: same shape but
 * targeting ``oauth-tools-pu`` (per_user_oauth). After Phase B the
 * admin-tab connect/disconnect endpoints apply the same single-slot
 * UX to per_user_oauth that admin_oauth has always had — at most
 * one admin "owns" the slot at a time, and B's connect 409s while
 * A holds it.
 *
 * Behaviours covered:
 *   - admin A connects via admin tab → slot_owner=A, ready=true
 *   - admin B sees A's badge from a separate session
 *   - admin B's connect 409s with "A is already connected"
 *   - admin B's admin-tab disconnect releases A's row, B can claim
 *   - per_user_oauth invocation rule: tool calls use the *caller's
 *     own* row, not the slot-owner field. Non-admin caller proves
 *     this most cleanly (their row exists alongside the admin's).
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import { apiLoginAs, makeMcpClient, mintMcpToken, OAUTH_TEST_MCP_URL as FAKE_OAUTH, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN_A = "admin@example.com";
const ADMIN_B = "admin2@example.com";
const NON_ADMIN = "alice@example.com";
const UPSTREAM = "oauth-tools-pu";

async function adminApi(request: APIRequestContext, email: string) {
  await apiLoginAs(request, email);
  return request;
}

async function getUpstream(api: APIRequestContext, id: string) {
  const resp = await api.get(`${BACKEND}/api/admin/upstreams/${id}`);
  expect(resp.status()).toBe(200);
  return resp.json();
}

async function slotOwner(api: APIRequestContext): Promise<string | null> {
  const detail = await getUpstream(api, UPSTREAM);
  return (detail.slot_owner as string | null) ?? null;
}

/**
 * Walk the admin-tab connect flow against the fake per_user_oauth
 * upstream, mirroring 15's ``completeAdminOauthConnect``. The
 * difference is that this spec hits the ``per_user_oauth`` slot in
 * the admin tab — Phase B made the take-over UX uniform across
 * both OAuth modes.
 */
async function completeAdminTabConnect(
  api: APIRequestContext,
  loggedInAs: string
) {
  const connectResp = await api.post(
    `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
  );
  expect(connectResp.status()).toBe(200);
  const body = await connectResp.json();
  if (body.connected) return;
  expect(body.authorization_url).toBeTruthy();

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

  await new Promise((r) => setTimeout(r, 200));
}

test.describe("per_user_oauth admin-tab single-slot take-over", () => {
  test.beforeEach(async ({ request }) => {
    await request.post(`${FAKE_OAUTH}/test/reset`);
    // Clear all rows on this upstream so each test starts from a
    // clean slate. Per-user disconnect only clears the calling
    // user's row, so we issue the call as each potential row holder.
    for (const email of [ADMIN_A, ADMIN_B, NON_ADMIN]) {
      await adminApi(request, email);
      await request.post(`${BACKEND}/api/auth/disconnect/${UPSTREAM}`);
    }
    // Also ensure no admin-tab side effects (enabled flag) linger.
    await adminApi(request, ADMIN_A);
    await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
    );
  });

  test("admin A connect -> slot_owner=A, ready=true", async ({ request }) => {
    await adminApi(request, ADMIN_A);
    expect(await slotOwner(request)).toBeNull();

    await completeAdminTabConnect(request, ADMIN_A);
    const detail = await getUpstream(request, UPSTREAM);
    expect(detail.slot_owner).toBe(ADMIN_A);
    expect(detail.ready).toBe(true);

    // Admin B sees the same slot owner (org-level state).
    await adminApi(request, ADMIN_B);
    expect(await slotOwner(request)).toBe(ADMIN_A);
  });

  test("admin B's connect 409s while A holds the slot", async ({
    request,
  }) => {
    await adminApi(request, ADMIN_A);
    await completeAdminTabConnect(request, ADMIN_A);

    await adminApi(request, ADMIN_B);
    const conflict = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
    );
    expect(conflict.status()).toBe(409);
    const detail = (await conflict.json()).detail;
    expect(detail).toContain(ADMIN_A);
    expect(detail.toLowerCase()).toContain("disconnect");
  });

  test("admin B's admin-tab disconnect releases the slot, then B can claim it", async ({
    request,
  }) => {
    await adminApi(request, ADMIN_A);
    await completeAdminTabConnect(request, ADMIN_A);

    await adminApi(request, ADMIN_B);
    const disconnect = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
    );
    expect(disconnect.status()).toBe(200);
    expect(await slotOwner(request)).toBeNull();

    await completeAdminTabConnect(request, ADMIN_B);
    expect(await slotOwner(request)).toBe(ADMIN_B);
  });

  test("per_user_oauth invocations always use the caller's own row", async ({
    request,
  }) => {
    // Both an admin (slot owner) and a non-admin sign in independently.
    await adminApi(request, ADMIN_A);
    await completeAdminTabConnect(request, ADMIN_A);

    // The non-admin signs in via /my-tools (the only door open to
    // them).
    await apiLoginAs(request, NON_ADMIN);
    const userConnect = await request.get(
      `${BACKEND}/api/auth/connect/${UPSTREAM}`
    );
    const body = await userConnect.json();
    if (!body.connected) {
      const authorizeUrl = new URL(body.authorization_url);
      authorizeUrl.searchParams.set("email", NON_ADMIN);
      const authorizeResp = await request.get(authorizeUrl.toString(), {
        maxRedirects: 0,
      });
      const callbackLoc = authorizeResp.headers()["location"];
      await request.get(callbackLoc, { maxRedirects: 0 });
      await new Promise((r) => setTimeout(r, 200));
    }

    // Non-admin's call: uses their own row (not the slot owner's).
    const tokenC = await mintMcpToken(request, NON_ADMIN, ORG);
    let mcp = await makeMcpClient(tokenC, ORG, "mcp");
    try {
      const result = await mcp.callTool({
        name: `${ORG}__${UPSTREAM}__secret_echo`,
        arguments: { message: "hi" },
      });
      const content = result.content as Array<{ type: string; text?: string }>;
      const text = content.find((c) => c.type === "text")?.text ?? "";
      expect(text).toContain("hi");
      expect(text).toContain(`as=${NON_ADMIN}`);
    } finally {
      await mcp.close();
    }

    // Slot owner's call: also uses their own row. (For the slot
    // owner the ``as=`` field happens to equal slot_owner — pin the
    // per-user invocation rule explicitly so a future regression
    // that confuses slot_owner with effective_user gets caught.)
    const tokenA = await mintMcpToken(request, ADMIN_A, ORG);
    mcp = await makeMcpClient(tokenA, ORG, "mcp");
    try {
      const result = await mcp.callTool({
        name: `${ORG}__${UPSTREAM}__secret_echo`,
        arguments: { message: "hi-A" },
      });
      const content = result.content as Array<{ type: string; text?: string }>;
      const text = content.find((c) => c.type === "text")?.text ?? "";
      expect(text).toContain(`as=${ADMIN_A}`);
    } finally {
      await mcp.close();
    }
  });
});
