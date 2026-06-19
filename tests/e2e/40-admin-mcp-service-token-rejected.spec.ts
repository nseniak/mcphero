/**
 * E2E-3 — a service token (``svct_``) is structurally rejected on the
 * slug-scoped admin MCP at the full stack.
 *
 * Mirrors the unit test
 * (backend/tests/unit/test_admin_mcp_service_token_rejection.py) at the
 * real network edge: the ``/admin-mcp`` mount wraps the *raw* OAuth
 * provider, which never consults the service-token registry, so a
 * ``svct_`` bearer fails ``verify_token`` and the request 401s before
 * any admin handler runs. This is the production geometry — the e2e
 * stack runs in cloud mode, so ``/admin-mcp/<slug>/`` travels the
 * genuine slug-resolve → rewrite path through OrgContextMiddleware.
 *
 * Two probes, both at the live edge:
 *   (a) a low-level POST initialize → asserts HTTP 401 (the structural
 *       rejection) with a ``WWW-Authenticate`` challenge header;
 *   (b) the MCP SDK client pointed at ``/admin-mcp/<slug>/`` →
 *       ``connect`` rejects (the SDK surfaces the 401 as a thrown
 *       error), confirming a real MCP client can't get in either.
 *
 * Co-location-safe: the token is minted and revoked within the test;
 * ``revokeQuietly`` in a try/finally covers Playwright's retry
 * re-entering between mint and revoke.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import {
  apiLoginAs,
  makeMcpClientAtPath,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const LABEL = "e2e-admin-mcp-reject";

async function revokeQuietly(api: APIRequestContext, label: string) {
  await api
    .delete(`${BACKEND}/api/admin/service-tokens/${label}`)
    .catch(() => {});
}

test("svct_ bearer is 401'd on /admin-mcp/<slug>/ at the full stack", async ({
  request,
}) => {
  await apiLoginAs(request, ADMIN);
  await revokeQuietly(request, LABEL);

  // Mint an admin-role service token — the worst case: even an
  // admin-scoped svct_ must not unlock the admin MCP.
  const mintResp = await request.post(
    `${BACKEND}/api/admin/service-tokens`,
    { data: { label: LABEL, role: "admin" } },
  );
  expect(mintResp.status()).toBe(201);
  const minted = await mintResp.json();
  expect(minted.token).toMatch(/^svct_/);

  try {
    // (a) Low-level probe: a raw initialize POST must 401 with an auth
    // challenge. ``maxRedirects: 0`` so a slug-rewrite redirect (if any)
    // doesn't mask the status the client would actually see.
    const probe = await request.post(`${BACKEND}/admin-mcp/${ORG}/`, {
      headers: {
        Authorization: `Bearer ${minted.token}`,
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
      },
      data: {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: {},
          clientInfo: { name: "e2e-probe", version: "0" },
        },
      },
      maxRedirects: 0,
    });
    expect(probe.status()).toBe(401);
    // Structural auth-challenge: a 401 here carries WWW-Authenticate
    // (the OAuth-provider boundary), never a JSON-RPC success body.
    const wwwAuth = probe.headers()["www-authenticate"];
    expect(wwwAuth, "401 must carry a WWW-Authenticate challenge").toBeTruthy();

    // (b) Real MCP client: pointing the SDK at the admin mount with a
    // svct_ bearer must fail to connect (the SDK throws on the 401).
    await expect(
      makeMcpClientAtPath(minted.token, `admin-mcp/${ORG}/`),
    ).rejects.toThrow();
  } finally {
    await revokeQuietly(request, LABEL);
  }
});
