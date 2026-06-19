/**
 * E2E-5 — revoking a service token mid-session blocks the *next* call
 * on the already-open client (per-request auth, not per-connection).
 *
 * 38-service-tokens asserts revocation bites on the next *connection*
 * (a fresh ``makeMcpClientAtPath`` rejects). This spec closes the
 * stronger gap: a client that is already connected and has made a
 * successful call must NOT keep working after the token is revoked —
 * the gateway re-authenticates every request, so the next ``callTool``
 * on the same live session is rejected. A per-connection-only check
 * would pass even if a live session outlived its credential.
 *
 * Co-location-safe: token minted + revoked within the test, with a
 * try/finally ``revokeQuietly`` covering Playwright retry re-entry.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";

import {
  apiLoginAs,
  makeMcpClientAtPath,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const LABEL = "e2e-revoke-mid-session";

async function revokeQuietly(api: APIRequestContext, label: string) {
  await api
    .delete(`${BACKEND}/api/admin/service-tokens/${label}`)
    .catch(() => {});
}

test("revoking a service token mid-session rejects the next call on the live client", async ({
  request,
}) => {
  await apiLoginAs(request, ADMIN);
  await revokeQuietly(request, LABEL);

  const mintResp = await request.post(
    `${BACKEND}/api/admin/service-tokens`,
    { data: { label: LABEL, role: "user" } },
  );
  expect(mintResp.status()).toBe(201);
  const minted = await mintResp.json();
  expect(minted.token).toMatch(/^svct_/);

  let client: Client | null = null;
  try {
    // Open a live session and prove it works before revocation — the
    // seeded service_account upstream's echo tool is the stable target
    // (no OAuth slot dependency).
    client = await makeMcpClientAtPath(minted.token, `mcp/${ORG}/`);
    const okResp = await client.callTool({
      name: "test-tools__echo",
      arguments: { message: "before-revoke" },
    });
    expect(JSON.stringify(okResp.content)).toContain("before-revoke");

    // Revoke through the API while the client is still connected.
    const revokeResp = await request.delete(
      `${BACKEND}/api/admin/service-tokens/${LABEL}`,
    );
    expect([200, 204]).toContain(revokeResp.status());

    // The NEXT call on the SAME live client must be rejected. The
    // gateway re-checks the bearer per request, so the revoked token
    // no longer authenticates. The rejection may surface either as a
    // thrown McpError (transport/auth layer rejects) — the dominant
    // case for a now-401'd bearer — or, defensively, as a tool-result
    // error payload. Both are "blocked"; a silent success echoing the
    // argument is the only failure.
    let blocked = false;
    let echoedAfterRevoke = false;
    try {
      const afterResp = await client.callTool({
        name: "test-tools__echo",
        arguments: { message: "after-revoke" },
      });
      const body = JSON.stringify(afterResp.content);
      echoedAfterRevoke = body.includes("after-revoke");
      blocked = Boolean(afterResp.isError) || !echoedAfterRevoke;
    } catch {
      blocked = true;
    }
    expect(echoedAfterRevoke).toBe(false);
    expect(blocked).toBe(true);
  } finally {
    if (client) await client.close().catch(() => {});
    await revokeQuietly(request, LABEL);
  }
});
