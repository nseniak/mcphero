/**
 * E2E-1 — upstream OAuth access-token expiry → silent refresh keeps
 * the tool working (full-stack, admin_oauth single-slot path).
 *
 * This is the stale-session incident class, driven end-to-end through
 * the gateway. Complements the 18a silent-refresh spec, which drives
 * the per_user_oauth (``oauth-tools-pu``) refresh path: here we hold
 * the ``oauth-tools`` admin_oauth slot, whose gateway token resolution
 * is a distinct code branch (pooled admin token, not the caller's own
 * row).
 *
 * Flow:
 *   1. Reset + drop the upstream access-token TTL to 1s, complete an
 *      admin_oauth connect → slot held by admin with a short-lived
 *      access token (refresh token long-lived).
 *   2. Warm-up ``secret_echo`` call → succeeds.
 *   3. Wait past the access token's TTL.
 *   4. Second ``secret_echo`` call → must STILL succeed, via a silent
 *      ``grant_type=refresh_token`` exchange (asserted via the fake's
 *      ``refresh_grant_count`` delta — proves a refresh actually
 *      happened, not a fresh handshake or a still-valid token).
 *   5. Dashboard truth: the admin upstream row stays ``ready`` with
 *      the admin as ``slot_owner`` — the session never went stale.
 */
import { test, expect } from "@playwright/test";

import {
  ADMIN,
  POST_EXPIRY_SLEEP_MS,
  resetArmAndConnect,
  callSecretEcho,
  fakeState,
  adminUpstreamRow,
} from "./_oauth_refresh_helpers";

test("admin_oauth: expired access token silently refreshes, tool keeps working", async ({
  request,
}) => {
  await resetArmAndConnect(request);

  const before = await callSecretEcho(request, ADMIN, "warmup");
  expect(before.isError).toBe(false);
  expect(before.text).toContain(`as=${ADMIN}`);

  const stateBefore = await fakeState(request);

  // Let the access token expire. The MCP SDK reads ``expires_at`` from
  // the gateway's token store; once it's in the past the next call
  // swaps to the refresh grant before sending (proactive, not 401-
  // driven).
  await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));

  const after = await callSecretEcho(request, ADMIN, "after-expiry");
  expect(after.isError).toBe(false);
  expect(after.text).toContain(`as=${ADMIN}`);
  expect(after.text).toContain("after-expiry");

  // Strong signal: a refresh actually happened. Without this delta the
  // test would pass even if the token simply hadn't expired yet, or if
  // the gateway re-ran a full OAuth handshake (a silent-refresh
  // regression).
  const stateAfter = await fakeState(request);
  expect(stateAfter.refresh_grant_count).toBeGreaterThan(
    stateBefore.refresh_grant_count,
  );

  // Dashboard truth: the admin tab still renders the row as Ready,
  // owned by the admin — the silent refresh kept the slot live. This
  // is the data backing the StatusBadge / upstreamStatusLabel render.
  const row = await adminUpstreamRow(request);
  expect(row.ready).toBe(true);
  expect(row.slot_owner).toBe(ADMIN);
});
