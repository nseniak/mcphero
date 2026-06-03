/**
 * Token-refresh — transient-blip path.
 *
 * §5.1 transient-keep policy: a 5xx on refresh-grant is a server
 * blip, not a user-credential issue. The gateway must keep the token
 * row so a subsequent attempt — once the upstream recovers —
 * succeeds silently. Without this guard, every minute of upstream
 * flakiness forces every user to re-auth.
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

test("transient refresh failure keeps tokens, retry succeeds without re-auth", async ({
  request,
}) => {
  test.setTimeout(60_000);

  await resetAndArm(request);

  // Baseline: tool call works with the freshly minted token.
  const ok = await callSecretEcho(request, USER, "before-blip");
  expect(ok.isError).toBe(false);

  // Wait past TTL so the SDK enters the refresh branch on the next
  // call, then arm a 503 on the very next refresh-grant.
  await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));
  const armResp = await request.post(
    `${OAUTH_TEST_MCP_URL}/test/fail-next-refresh`,
    { form: { count: "1" } }
  );
  expect(armResp.status()).toBe(200);

  // Tool call during the blip: surfaces as an error from the user's
  // perspective (refresh failed → no usable token).
  const blipped = await callSecretEcho(request, USER, "during-blip");
  expect(blipped.isError || blipped.threw !== null).toBe(true);

  // §5.1 invariant: tokens are NOT deleted on a transient signature.
  // The proof: ``/api/auth/connect`` still fast-paths to
  // ``connected=true`` (no authorization_url) because the stored
  // refresh token is still on disk and now-recovered ``/token``
  // accepts it.
  const recoverAttempt = await request.get(
    `${BACKEND_URL}/api/auth/connect/${UPSTREAM}`
  );
  expect(recoverAttempt.status()).toBe(200);
  const recoverBody = await recoverAttempt.json();
  expect(recoverBody.connected).toBe(true);
  expect(recoverBody.authorization_url).toBeFalsy();

  // And the next tool call goes through cleanly — no re-auth dance
  // was needed; the upstream simply recovered between the two
  // attempts.
  const after = await callSecretEcho(request, USER, "after-blip");
  expect(after.isError).toBe(false);
  expect(after.text).toContain(`as=${USER}`);
  expect(after.text).toContain("after-blip");
});
