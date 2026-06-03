/**
 * Shared helpers for the 18*-token-refresh.spec.ts trilogy. Extracted
 * out of the original single-file spec when we split it for
 * shard-level parallelism.
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
export const USER = "admin@example.com";
export const UPSTREAM = "oauth-tools-pu";

// Tight enough to keep the suite fast (the 1s floor is enforced by
// the fake's set-token-ttl endpoint), with a 1.5s sleep below to give
// the SDK a 50% margin past expiry. Lowering further trades a few
// hundred ms of runtime for flake risk on a slow machine.
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

export async function completeUserOauth(
  api: APIRequestContext,
  email: string
): Promise<void> {
  const connectResp = await api.get(
    `${BACKEND_URL}/api/auth/connect/${UPSTREAM}`
  );
  expect(connectResp.status()).toBe(200);
  const body = await connectResp.json();
  if (body.connected) return;
  const authorizeUrl = new URL(body.authorization_url);
  authorizeUrl.searchParams.set("email", email);
  const authorizeResp = await api.get(authorizeUrl.toString(), {
    maxRedirects: 0,
  });
  expect(authorizeResp.status()).toBe(302);
  const callbackLoc = authorizeResp.headers()["location"];
  await api.get(callbackLoc, { maxRedirects: 0 });
  await new Promise((r) => setTimeout(r, 200));
}

export async function callSecretEcho(
  request: APIRequestContext,
  email: string,
  message: string,
  timeoutMs: number = 10_000
): Promise<{ text: string; isError: boolean; threw: string | null }> {
  const token = await mintMcpToken(request, email, ORG);
  const mcp = await makeMcpClient(token, ORG, "mcp");
  const timeoutPromise = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error(`callTool timed out after ${timeoutMs}ms`)), timeoutMs)
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
      // The gateway may surface complete auth-failure as a thrown
      // McpError rather than an ``isError: true`` payload. The
      // ``timeoutMs`` race also lands here when a re-auth-required
      // upstream causes the SDK to hang on a refresh-grant retry —
      // that's still a "surfaced re-auth" outcome from the user's
      // perspective (the call doesn't silently succeed).
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
      // close() can throw on a session that's already torn down
      // by the upstream's 401 — irrelevant to the assertion.
    }
  }
}

export async function resetAndArm(request: APIRequestContext) {
  await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
  await apiLoginAs(request, USER);
  await request.post(`${BACKEND_URL}/api/auth/disconnect/${UPSTREAM}`);
  const ttlResp = await request.post(
    `${OAUTH_TEST_MCP_URL}/test/set-token-ttl`,
    { form: { seconds: String(TOKEN_TTL_SECONDS) } }
  );
  expect(ttlResp.status()).toBe(200);
  await apiLoginAs(request, USER);
  await completeUserOauth(request, USER);
}
