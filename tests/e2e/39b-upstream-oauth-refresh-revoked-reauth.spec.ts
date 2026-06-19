/**
 * E2E-2 — upstream OAuth tokens revoked → actionable re-auth surfaced
 * (full-stack, admin_oauth single-slot path).
 *
 * Complements 18b (per_user_oauth total-revocation, which proves the
 * re-auth signal + token deletion via the ``/api/auth/connect``
 * *reconnect* route). This spec exercises the **gateway tool-call**
 * path on the ``oauth-tools`` admin_oauth upstream: we revoke both
 * token classes while the admin holds the slot, then call a tool
 * through ``/mcp``.
 *
 * Two distinct outcomes are asserted in two tests:
 *
 *   1. (active) The gateway surfaces a clean needs-re-authentication
 *      signal on the next tool call — NOT a silent ``as=<email>``
 *      success, NOT a raw 500. This is the user-facing guardrail and
 *      it PASSES.
 *
 *   2. (test.fixme — candidate bug) The dashboard reflects the
 *      disconnected state. It currently does NOT, via this path: see
 *      the long comment on the fixme'd test below. Reported as a
 *      candidate bug for
 *      plans/auth-invocation-gateway-test-audit-bugs.md.
 */
import { test, expect } from "@playwright/test";

import {
  ORG,
  ADMIN,
  UPSTREAM,
  POST_EXPIRY_SLEEP_MS,
  resetArmAndConnect,
  callSecretEcho,
  adminUpstreamRow,
} from "./_oauth_refresh_helpers";
import { loginAs, OAUTH_TEST_MCP_URL, BACKEND_URL } from "./helpers";

test("admin_oauth: total token revocation surfaces a re-auth signal on the next tool call", async ({
  request,
}) => {
  // The reconnect-with-stored-tokens path can approach the MCP SDK's
  // default request timeout when the upstream rejects the refresh.
  // Give Playwright headroom so the gateway's own timeout fires first.
  test.setTimeout(60_000);

  await resetArmAndConnect(request);

  const ok = await callSecretEcho(request, ADMIN, "before-revoke");
  expect(ok.isError).toBe(false);
  expect(ok.text).toContain(`as=${ADMIN}`);

  // Wait past TTL so the SDK enters the refresh branch, then revoke
  // BOTH token classes so the refresh-grant attempt 401s too.
  await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));
  const revokeResp = await request.post(
    `${OAUTH_TEST_MCP_URL}/test/revoke-all-tokens`,
  );
  expect(revokeResp.status()).toBe(200);

  // The gateway must surface a clear "needs re-auth" signal — a
  // tool-result error flag or a re-auth-shaped textual message — and
  // crucially NOT a silent success echoing the revoked identity.
  const failed = await callSecretEcho(request, ADMIN, "after-total-revoke");
  const lowered = failed.text.toLowerCase();
  const surfaced =
    failed.isError ||
    lowered.includes("not signed in") ||
    lowered.includes("authenticate") ||
    lowered.includes("not currently available") ||
    lowered.includes("please tell");
  expect(surfaced).toBe(true);
  expect(failed.text).not.toMatch(/as=admin@example\.com.*after-total-revoke/);

  // After the gateway observes the dead tokens, ``/connect`` must no
  // longer fast-path to ``connected=true`` — re-auth is required.
  // (This re-auth route DOES drop the dead tokens; the dashboard-
  // ``ready`` regression below is specific to the *tool-call* path
  // before any reconnect route runs.)
  const reconnectAttempt = await request.post(
    `${BACKEND_URL}/api/admin/upstreams/${UPSTREAM}/connect`,
  );
  expect(reconnectAttempt.status()).toBe(200);
  const reconnectBody = await reconnectAttempt.json();
  expect(reconnectBody.connected).toBeFalsy();
  expect(reconnectBody.authorization_url).toBeTruthy();
});

// CANDIDATE BUG (reported, not a test bug). On the gateway *tool-call*
// path, a total upstream-token revocation does NOT flip the admin
// dashboard's ``ready`` for an admin_oauth upstream. Observed in a real
// e2e run (/tmp/mcpolis-e2e-shard-0.log): the failed call raised
// ``SilentReconnectAuthRequired`` and the gateway surfaced re-auth to
// the user correctly — but NO ``tokens_deleted`` log line followed, and
// ``GET /api/admin/upstreams`` kept returning ``ready: true`` for 10s+
// of polling. Root cause (code-read, corroborated by the log): the §5.1
// invalid_grant token-deletion runs only inside
// ``_classify_reconnect_failure`` on the *reconnect* paths
// (``/api/admin/upstreams/<id>/connect`` and ``/api/auth/connect``);
// the tool_router's failure path raises ``UpstreamRouterError`` to
// surface re-auth but never deletes the slot owner's stored token row.
// Since ``resolve_upstream_readiness`` defines admin_oauth readiness as
// "an admin holds a token row" (_deps.py:181), the row's survival keeps
// the admin tab showing a green "Ready" badge + a Disconnect button —
// even though every tool call fails with re-auth — until an admin
// manually clicks Connect/Disconnect or a periodic reconnect runs. The
// intended behavior (and what 18b proves for the reconnect route) is
// that the dashboard reflects the disconnected / re-auth-needed state.
//
//   Observed:  admin tab row stays ready=true after tool-call-path
//              revocation; detail page shows "Ready" + Disconnect.
//   Intended:  ready=false (re-auth needed); detail page shows the
//              Authenticate affordance, no green "Ready" badge.
//   Where:     tool_router.py failure path (no token-row deletion) vs
//              upstream_connection_service.py:_classify_reconnect_failure
//              (§5.1 delete) + _deps.py:resolve_upstream_readiness.
test(
  "admin_oauth: total token revocation disconnects the dashboard (tool-call path)",
  async ({ page, request }) => {
    test.setTimeout(60_000);

    await resetArmAndConnect(request);

    const ok = await callSecretEcho(request, ADMIN, "before-revoke");
    expect(ok.isError).toBe(false);

    await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));
    await request.post(`${OAUTH_TEST_MCP_URL}/test/revoke-all-tokens`);

    // Trigger the gateway tool-call failure (re-auth surfaced).
    await callSecretEcho(request, ADMIN, "after-total-revoke");

    // EXPECTED once fixed: the admin upstream row flips to not-ready
    // because the dead token row is dropped.
    await expect
      .poll(async () => (await adminUpstreamRow(request)).ready, {
        timeout: 10_000,
        intervals: [250, 500, 1000],
      })
      .toBe(false);

    // EXPECTED once fixed: the detail page renders the re-auth
    // affordance (Authenticate) and no green "Ready" badge.
    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM}`);
    await expect(
      page.getByRole("heading", { name: "OAuth Tools", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /Authenticate/i }),
    ).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("Ready", { exact: true })).toHaveCount(0);
  },
);
