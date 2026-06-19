/**
 * Shared fixtures for the 39*-upstream-oauth full-stack guardrail
 * specs (E2E-1 token-expiry silent refresh, E2E-2 refresh-token
 * revoked → re-auth surfaced).
 *
 * These specs target the **admin_oauth** upstream (``oauth-tools``),
 * which complements the existing 18*-token-refresh trilogy: 18a/18b
 * drive the ``oauth-tools-pu`` (per_user_oauth) refresh path, while
 * the admin_oauth single-slot refresh path is a distinct code branch
 * (the gateway resolves a pooled admin token rather than the caller's
 * own row). Same fake provider, same TTL/revocation test knobs.
 *
 * Co-location-safe: every entry point resets the fake provider and the
 * MCPolis-side slot before arming, so a prior spec in the shard can't
 * leak a queued email, a stale TTL, or a connected slot.
 */
import { expect, type APIRequestContext } from "@playwright/test";

import {
  apiLoginAs,
  makeMcpClient,
  mintMcpToken,
  OAUTH_TEST_MCP_URL,
  BACKEND_URL,
} from "./helpers";

export const ORG = "acme-corp";
export const ADMIN = "admin@example.com";
export const UPSTREAM = "oauth-tools";

// 1s is the fake's enforced TTL floor; the 1.5s sleep gives the SDK a
// 50% margin past expiry before the next call so its proactive
// expiry-based refresh path fires deterministically. Same values the
// 18*-token-refresh helpers use.
export const TOKEN_TTL_SECONDS = 1;
export const POST_EXPIRY_SLEEP_MS = 1500;

export interface FakeState {
  active_access_tokens: number;
  active_refresh_tokens: number;
  refresh_grant_count: number;
  email_queue_depth: number;
}

export async function fakeState(api: APIRequestContext): Promise<FakeState> {
  const resp = await api.get(`${OAUTH_TEST_MCP_URL}/test/state`);
  expect(resp.status()).toBe(200);
  return (await resp.json()) as FakeState;
}

/**
 * Walk MCPolis's admin_oauth connect flow end-to-end against the fake
 * provider. Returns once the slot is populated for ``loggedInAs``.
 * Mirrors ``completeAdminOauthConnect`` from 15-admin-oauth-takeover.
 */
export async function completeAdminOauthConnect(
  api: APIRequestContext,
  loggedInAs: string,
): Promise<void> {
  const connectResp = await api.post(
    `${BACKEND_URL}/api/admin/upstreams/${UPSTREAM}/connect`,
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

  // Same fire-and-forget settle window the admin_oauth spec uses: the
  // callback schedules the token-store write + tool re-discovery, and
  // an immediate read can race them.
  await new Promise((r) => setTimeout(r, 200));
}

/**
 * Call ``secret_echo`` through the gateway as ``email``. Returns the
 * normalized result shape (text / isError / threw) used by all the
 * refresh specs. The ``timeoutMs`` race converts a hung refresh-grant
 * retry into a "surfaced re-auth" outcome rather than a Playwright
 * timeout — that's still a non-silent-success from the user's view.
 */
export async function callSecretEcho(
  request: APIRequestContext,
  email: string,
  message: string,
  timeoutMs = 10_000,
): Promise<{ text: string; isError: boolean; threw: string | null }> {
  const token = await mintMcpToken(request, email, ORG);
  const mcp = await makeMcpClient(token, ORG, "mcp");
  const timeoutPromise = new Promise<never>((_, reject) =>
    setTimeout(
      () => reject(new Error(`callTool timed out after ${timeoutMs}ms`)),
      timeoutMs,
    ),
  );
  try {
    try {
      const result = await Promise.race([
        mcp.callTool({
          name: `${ORG}__${UPSTREAM}__secret_echo`,
          arguments: { message },
        }),
        timeoutPromise,
      ]);
      const content = result.content as Array<{ type: string; text?: string }>;
      return {
        text: content.find((c) => c.type === "text")?.text ?? "",
        isError: Boolean(result.isError),
        threw: null,
      };
    } catch (err) {
      return {
        text: "",
        isError: true,
        threw: err instanceof Error ? err.message : String(err),
      };
    }
  } finally {
    try {
      await mcp.close();
    } catch {
      // close() can throw on a session the upstream already tore down
      // with a 401 — irrelevant to the assertion.
    }
  }
}

/**
 * Read the admin upstream summary row for ``UPSTREAM``. Used for the
 * dashboard-state assertions (``ready`` / ``slot_owner``) — the
 * data-backing of what the admin tab's StatusBadge renders.
 */
export async function adminUpstreamRow(
  api: APIRequestContext,
): Promise<{ id: string; ready: boolean; slot_owner: string | null }> {
  const resp = await api.get(`${BACKEND_URL}/api/admin/upstreams`);
  expect(resp.status()).toBe(200);
  const rows = (await resp.json()) as Array<{
    id: string;
    ready: boolean;
    slot_owner: string | null;
  }>;
  const row = rows.find((u) => u.id === UPSTREAM);
  expect(row, `upstream ${UPSTREAM} missing from admin list`).toBeTruthy();
  return row!;
}

/**
 * Reset both sides of the fake-vs-MCPolis state, drop the access-token
 * TTL to the short value, then complete a fresh admin_oauth connect so
 * the slot is held by ``ADMIN`` with a soon-to-expire access token.
 */
export async function resetArmAndConnect(
  request: APIRequestContext,
): Promise<void> {
  await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
  await apiLoginAs(request, ADMIN);
  await request.post(
    `${BACKEND_URL}/api/admin/upstreams/${UPSTREAM}/disconnect`,
  );
  const ttlResp = await request.post(
    `${OAUTH_TEST_MCP_URL}/test/set-token-ttl`,
    { form: { seconds: String(TOKEN_TTL_SECONDS) } },
  );
  expect(ttlResp.status()).toBe(200);
  await apiLoginAs(request, ADMIN);
  await completeAdminOauthConnect(request, ADMIN);
}
