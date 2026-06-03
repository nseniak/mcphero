/**
 * Per-user OAuth: each user has an independent token.
 *
 * Sibling spec to 15-admin-oauth-takeover. Same fake upstream
 * (tests/e2e/oauth_test_mcp_server.py) but configured as
 * ``auth_mode=per_user_oauth`` — the gateway forwards the *caller's*
 * own token to the upstream (no admin pool), and a disconnect only
 * affects the disconnecting user's row.
 *
 * Behaviours covered:
 *   - user A and user B each complete OAuth independently
 *   - tools forwarded through the gateway carry the caller's token
 *     (proven via the fake's ``as=<email>`` echo)
 *   - user A's disconnect clears only A's row; user B keeps working
 *   - a user with no stored token can't call the tool
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import { apiLoginAs, makeMcpClient, mintMcpToken, OAUTH_TEST_MCP_URL, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const USER_A = "admin@example.com";
const USER_B = "admin2@example.com";
const UPSTREAM = "oauth-tools-pu";

async function userApi(request: APIRequestContext, email: string) {
  await apiLoginAs(request, email);
  return request;
}

/**
 * Walk the per-user OAuth flow end-to-end against the fake provider.
 * Mirrors ``completeAdminOauthConnect`` from the admin-oauth spec but
 * targets the ``GET /api/auth/connect/<id>`` route — the per_user
 * variant — instead of ``POST /api/admin/upstreams/<id>/connect``.
 */
async function completeUserOauthConnect(
  api: APIRequestContext,
  loggedInAs: string
) {
  const connectResp = await api.get(
    `${BACKEND}/api/auth/connect/${UPSTREAM}`
  );
  expect(connectResp.status()).toBe(200);
  const body = await connectResp.json();
  if (body.connected) {
    return;
  }
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

  // Same fire-and-forget settle window as the admin_oauth spec.
  await new Promise((r) => setTimeout(r, 200));
}

async function callSecretEcho(
  request: APIRequestContext,
  callerEmail: string,
  message: string
): Promise<{ text: string; isError: boolean }> {
  const token = await mintMcpToken(request, callerEmail, ORG);
  const mcp = await makeMcpClient(token, ORG, "mcp");
  try {
    const result = await mcp.callTool({
      name: `${ORG}__${UPSTREAM}__secret_echo`,
      arguments: { message },
    });
    const content = result.content as Array<{ type: string; text?: string }>;
    return {
      text: content.find((c) => c.type === "text")?.text ?? "",
      isError: Boolean(result.isError),
    };
  } finally {
    await mcp.close();
  }
}

test.describe("per_user_oauth: independent tokens per user", () => {
  test.beforeEach(async ({ request }) => {
    // Reset both users' rows so each test starts clean. Per_user
    // disconnect only clears the calling user's token, so we have
    // to do this for each user. Also wipe fake-provider state so
    // a previous spec's TTL/queue can't leak in.
    await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
    await userApi(request, USER_A);
    await request.post(`${BACKEND}/api/auth/disconnect/${UPSTREAM}`);
    await userApi(request, USER_B);
    await request.post(`${BACKEND}/api/auth/disconnect/${UPSTREAM}`);
  });

  test("each user's tool call uses their own upstream token", async ({
    request,
  }) => {
    // Both users complete OAuth independently.
    await userApi(request, USER_A);
    await completeUserOauthConnect(request, USER_A);

    // ``/api/user/mcps`` is the /my-tools surface. Under the unified
    // readiness model, ``ready`` is the org-level "an admin has
    // authenticated" gate (USER_A is admin, so once they sign in the
    // upstream is Ready for everyone). The per-viewer signed-in
    // state lives in ``user_connection_status``: USER_A's own row
    // gives them ``connected``, USER_B has no row yet so they see
    // ``not_connected`` until they complete OAuth themselves.
    const aBefore = (await (
      await request.get(`${BACKEND}/api/user/mcps`)
    ).json()).find((m: { id: string }) => m.id === UPSTREAM);
    expect(aBefore.ready).toBe(true);
    expect(aBefore.user_connection_status).toBe("connected");

    await userApi(request, USER_B);
    const bBeforeConnect = (await (
      await request.get(`${BACKEND}/api/user/mcps`)
    ).json()).find((m: { id: string }) => m.id === UPSTREAM);
    // USER_A (admin) is still signed in, so the upstream is Ready
    // for B too — but B's per-viewer status reflects their (absent)
    // own row.
    expect(bBeforeConnect.ready).toBe(true);
    expect(bBeforeConnect.user_connection_status).toBe("not_connected");

    await completeUserOauthConnect(request, USER_B);

    const bAfter = (await (
      await request.get(`${BACKEND}/api/user/mcps`)
    ).json()).find((m: { id: string }) => m.id === UPSTREAM);
    expect(bAfter.ready).toBe(true);
    expect(bAfter.user_connection_status).toBe("connected");

    // User A's call → ``as=A`` (the upstream sees A's stored token).
    const aResult = await callSecretEcho(request, USER_A, "from-A");
    expect(aResult.isError).toBe(false);
    expect(aResult.text).toContain("from-A");
    expect(aResult.text).toContain(`as=${USER_A}`);

    // User B's call → ``as=B``. Crucially, A's connection does NOT
    // leak — admin_oauth's pool semantics aren't in play here.
    const bResult = await callSecretEcho(request, USER_B, "from-B");
    expect(bResult.isError).toBe(false);
    expect(bResult.text).toContain("from-B");
    expect(bResult.text).toContain(`as=${USER_B}`);
  });

  test("user A's disconnect leaves user B's token intact", async ({
    request,
  }) => {
    await userApi(request, USER_A);
    await completeUserOauthConnect(request, USER_A);
    await userApi(request, USER_B);
    await completeUserOauthConnect(request, USER_B);

    // A disconnects.
    await userApi(request, USER_A);
    const disconnect = await request.post(
      `${BACKEND}/api/auth/disconnect/${UPSTREAM}`
    );
    expect(disconnect.status()).toBe(200);

    // B's tool call still works — independent rows.
    const bResult = await callSecretEcho(request, USER_B, "still-here");
    expect(bResult.isError).toBe(false);
    expect(bResult.text).toContain(`as=${USER_B}`);

    // A's tool call now reflects "no stored token". The exact error
    // shape depends on the gateway's user-not-connected handler;
    // either ``isError`` or a "not currently available" text is
    // acceptable. The strong assertion is that A is not somehow
    // resolving via B's token.
    const aResult = await callSecretEcho(request, USER_A, "should-fail");
    const lowered = aResult.text.toLowerCase();
    const surfaced =
      aResult.isError ||
      lowered.includes("not signed in") ||
      lowered.includes("not currently available") ||
      lowered.includes("authenticate") ||
      lowered.includes("please tell");
    expect(surfaced).toBe(true);
    expect(aResult.text).not.toContain(`as=${USER_B}`);
  });

  test("after the user signs out, /my-tools reports the upstream disconnected", async ({
    request,
  }) => {
    // The /my-tools page shows freshly-disconnected per_user_oauth
    // upstreams as Unavailable. This is the round-trip the user
    // walks: add upstream (seeded), authenticate, switch to
    // /my-tools, sign out, refresh /my-tools. Pin every transition
    // so a regression in either the disconnect endpoint OR the
    // ``ready`` / ``user_connection_status`` fields is caught.
    //
    // USER_A is admin, so signing them in makes the upstream Ready;
    // signing them out (the only admin) drops Ready back to false.
    await userApi(request, USER_A);
    await completeUserOauthConnect(request, USER_A);

    // Pre-state: /my-tools shows the row Ready and Signed In.
    const beforeSignOut = (await (
      await request.get(`${BACKEND}/api/user/mcps`)
    ).json()).find((m: { id: string }) => m.id === UPSTREAM);
    expect(beforeSignOut.ready).toBe(true);
    expect(beforeSignOut.user_connection_status).toBe("connected");

    // Sign out from the upstream — same endpoint the /my-tools
    // "Sign out" button posts to.
    const disconnect = await request.post(
      `${BACKEND}/api/auth/disconnect/${UPSTREAM}`
    );
    expect(disconnect.status()).toBe(200);

    // Post-state: same /my-tools view now reports the row as
    // not Ready (no admin signed in). Both ``ready`` and the
    // per-viewer status are false; the frontend renders the row
    // as Unavailable.
    const afterSignOut = (await (
      await request.get(`${BACKEND}/api/user/mcps`)
    ).json()).find((m: { id: string }) => m.id === UPSTREAM);
    expect(afterSignOut.ready).toBe(false);
    expect(afterSignOut.user_connection_status).toBe("not_connected");
  });

  test("a user who has not connected cannot call the tool", async ({
    request,
  }) => {
    // No connect — the slot is reset by beforeEach. User A's call
    // must surface a clear "you need to authenticate" signal rather
    // than silently picking another user's token.
    const aResult = await callSecretEcho(request, USER_A, "no-token");
    const lowered = aResult.text.toLowerCase();
    const surfaced =
      aResult.isError ||
      lowered.includes("not signed in") ||
      lowered.includes("not currently available") ||
      lowered.includes("authenticate") ||
      lowered.includes("please tell");
    expect(surfaced).toBe(true);
    // Defensive: must not return an "as=" line at all (no token =
    // no upstream call).
    expect(aResult.text).not.toContain("as=");
  });
});
