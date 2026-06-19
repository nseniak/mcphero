/**
 * E2E-4 — an invalid gateway bearer is 401'd on ``/mcp``; a fresh
 * login re-establishes access (per-request gateway auth + recovery).
 *
 * NOTE on scope vs the brief. The brief asked to "mint a gateway token
 * with a short TTL via the test knob" and wait it out to observe a 401.
 * There is NO such knob: ``POST /api/auth/test-mcp-token`` always mints
 * with the fixed ``ACCESS_TOKEN_TTL`` (1h) and accepts no TTL override
 * (backend/src/.../routes/dashboard_auth.py:518, mint_test_token in
 * mcp_gateway_oauth_provider.py:534). Forcing a gateway bearer to
 * expire mid-test would require a backend/src change, which is out of
 * scope for this test-only task. We therefore assert the same
 * end-state the brief targets — "a bearer the gateway rejects yields a
 * 401, and re-authenticating restores access" — using an invalid
 * bearer rather than an expired one. The expiry-driven variant is
 * tracked as a candidate gap (a test-mode TTL knob on test-mcp-token)
 * in the bug report, not faked here.
 *
 * Both bearers travel the genuine gateway edge: the bare ``/mcp/``
 * mount, ServiceTokenOrgPinMiddleware, and the BearerAuthBackend.
 */
import { test, expect } from "@playwright/test";

import {
  apiLoginAs,
  mintMcpToken,
  makeMcpClient,
  BACKEND_URL as BACKEND,
} from "./helpers";

const ORG = "acme-corp";
const USER = "admin@example.com";

test("invalid gateway bearer is 401'd on /mcp; a fresh login restores access", async ({
  request,
}) => {
  // An obviously-invalid bearer (right shape, never minted) must be
  // rejected at the gateway with a 401 + auth challenge — proving the
  // gateway authenticates every request rather than trusting a
  // well-formed-looking token.
  const badProbe = await request.post(`${BACKEND}/mcp/${ORG}/`, {
    headers: {
      Authorization: "Bearer not-a-real-gateway-token-deadbeef",
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
  expect(badProbe.status()).toBe(401);
  expect(
    badProbe.headers()["www-authenticate"],
    "401 must carry a WWW-Authenticate challenge",
  ).toBeTruthy();

  // The MCP SDK pointed at the gateway with the bad bearer must fail to
  // connect (the 401 surfaces as a thrown error during the handshake).
  await expect(
    makeMcpClient("not-a-real-gateway-token-deadbeef", ORG, "mcp"),
  ).rejects.toThrow();

  // Recovery: a fresh dev-stub login + a freshly minted valid bearer
  // re-establishes access — the gateway connects and lists tools.
  await apiLoginAs(request, USER);
  const goodToken = await mintMcpToken(request, USER, ORG);
  const client = await makeMcpClient(goodToken, ORG, "mcp");
  try {
    const names = (await client.listTools()).tools.map((t) => t.name);
    // The seeded service_account upstream is always present and
    // doesn't depend on any OAuth slot, so its echo tool is a stable
    // proof that the authenticated session is live.
    expect(names).toContain(`${ORG}__test-tools__echo`);
  } finally {
    await client.close().catch(() => {});
  }
});
