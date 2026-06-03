/**
 * Token-refresh — total-revocation path.
 *
 * Asserts that when both access and refresh tokens are revoked at
 * the upstream, the gateway surfaces a clear, user-actionable error
 * (instead of silently failing or accepting the dead tokens) AND
 * deletes the stored refresh token server-side per RFC 6749 §5.1.
 *
 * Split out of the historic 18-token-refresh.spec.ts. See
 * ``_token_refresh_helpers.ts`` for shared fixtures.
 */
import { test, expect } from "@playwright/test";

import {
  USER,
  UPSTREAM,
  POST_EXPIRY_SLEEP_MS,
  resetAndArm,
  callSecretEcho,
} from "./_token_refresh_helpers";
import { OAUTH_TEST_MCP_URL, BACKEND_URL } from "./helpers";

test("total revocation (access + refresh) surfaces a re-auth error", async ({
  request,
}) => {
  // The reconnect-with-stored-tokens path can trip the MCP SDK's
  // 30-second-default request timeout when the upstream rejects
  // the refresh attempt. Give Playwright extra headroom so the
  // gateway's session-creation timeout fires *before* this test
  // gives up.
  test.setTimeout(60_000);

  await resetAndArm(request);

  const ok = await callSecretEcho(request, USER, "before-revoke");
  expect(ok.isError).toBe(false);

  // Wait past TTL so the SDK enters the refresh branch, then nuke
  // both token classes so the refresh attempt 401s too.
  await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));
  const revokeResp = await request.post(
    `${OAUTH_TEST_MCP_URL}/test/revoke-all-tokens`
  );
  expect(revokeResp.status()).toBe(200);

  // The gateway must surface a clear "needs re-auth" signal —
  // either a tool-result error flag or a "not signed in /
  // authenticate" textual message. The strict assertion is that
  // the response is NOT a silent ``as=<email>`` success (which
  // would mean the gateway accepted the revoked token).
  const failed = await callSecretEcho(request, USER, "after-total-revoke");
  const lowered = failed.text.toLowerCase();
  const surfaced =
    failed.isError ||
    lowered.includes("not signed in") ||
    lowered.includes("authenticate") ||
    lowered.includes("not currently available") ||
    lowered.includes("please tell");
  expect(surfaced).toBe(true);
  expect(failed.text).not.toMatch(/as=admin@example\.com.*after-total-revoke/);

  // §5.1 invalid_grant policy: tokens MUST be deleted server-side.
  // The strongest verification is that ``/api/auth/connect`` no
  // longer fast-paths to ``connected=true`` — it must hand back an
  // ``authorization_url`` (re-auth required). If tokens were not
  // deleted (regression of the invalid_grant branch in
  // ``_classify_reconnect_failure``) this would still fast-path.
  const reconnectAttempt = await request.get(
    `${BACKEND_URL}/api/auth/connect/${UPSTREAM}`
  );
  expect(reconnectAttempt.status()).toBe(200);
  const reconnectBody = await reconnectAttempt.json();
  expect(reconnectBody.connected).toBeFalsy();
  expect(reconnectBody.authorization_url).toBeTruthy();
});
