/**
 * Shared steps for the specs that drive the Admin MCP next to the
 * dashboard (46a-admin-mcp-caller-identity, 46b-admin-mcp-dashboard-parity).
 */
import { expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import {
  apiLoginAs,
  makeMcpClient,
  mintMcpToken,
  BACKEND_URL as BACKEND,
} from "./helpers";

export const ORG = "acme-corp";
export const ADMIN = "admin@example.com";
export const ADMIN_2 = "admin2@example.com";

/** Call one Admin MCP tool as ``email`` (a bearer token, as an admin's
 *  assistant would) and return its text answer. */
export async function callAdminTool(
  request: APIRequestContext,
  email: string,
  name: string,
  args: Record<string, unknown>,
): Promise<string> {
  const token = await mintMcpToken(request, email, ORG);
  const client: Client = await makeMcpClient(token, ORG, "admin-mcp");
  try {
    const result = await client.callTool({ name, arguments: args });
    const content = result.content as Array<{ type: string; text?: string }>;
    return content.find((c) => c.type === "text")?.text ?? "";
  } finally {
    await client.close().catch(() => {});
  }
}

/** The admin holding an upstream's single admin sign-in slot, as the
 *  dashboard shows it. Needs an admin's dashboard cookie on ``api``. */
export async function slotOwner(
  api: APIRequestContext,
  upstreamId: string,
): Promise<string | null> {
  const resp = await api.get(`${BACKEND}/api/admin/upstreams/${upstreamId}`);
  expect(resp.status()).toBe(200);
  return ((await resp.json()).slot_owner as string | null) ?? null;
}

/** Finish an OAuth sign-in the way the admin's browser would: open the
 *  authorization URL on the fake provider as ``email``, then follow its
 *  redirect to the upstream callback. */
export async function finishSignIn(
  api: APIRequestContext,
  authorizationUrl: string,
  email: string,
): Promise<void> {
  const authorizeUrl = new URL(authorizationUrl);
  authorizeUrl.searchParams.set("email", email);
  const authorizeResp = await api.get(authorizeUrl.toString(), {
    maxRedirects: 0,
  });
  expect(authorizeResp.status()).toBe(302);
  const callbackResp = await api.get(authorizeResp.headers()["location"], {
    maxRedirects: 0,
  });
  expect(callbackResp.status()).toBe(200);
}

/** Sign ``email`` in to ``upstreamId`` through the dashboard's Connect. */
export async function dashboardSignIn(
  api: APIRequestContext,
  upstreamId: string,
  email: string,
): Promise<void> {
  await apiLoginAs(api, email);
  const resp = await api.post(
    `${BACKEND}/api/admin/upstreams/${upstreamId}/connect`,
  );
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  if (!body.connected) {
    expect(body.authorization_url).toBeTruthy();
    await finishSignIn(api, body.authorization_url, email);
  }
  await expect.poll(() => slotOwner(api, upstreamId)).toBe(email);
}
