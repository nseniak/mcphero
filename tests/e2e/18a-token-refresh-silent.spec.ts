/**
 * Token-refresh — silent-refresh path.
 *
 * Asserts that an expired access token (refresh token still valid)
 * triggers a transparent ``grant_type=refresh_token`` exchange and
 * the in-flight tool call still succeeds.
 *
 * Split out of the historic 18-token-refresh.spec.ts trilogy so each
 * branch lands on its own spec file and the orchestrator can spread
 * the ~5s wait across shards. See ``_token_refresh_helpers.ts`` for
 * the shared fixture code.
 */
import { test, expect } from "@playwright/test";

import {
  USER,
  POST_EXPIRY_SLEEP_MS,
  resetAndArm,
  callSecretEcho,
  fakeState,
} from "./_token_refresh_helpers";

test("expired access token triggers silent refresh, tool call still succeeds", async ({
  request,
}) => {
  await resetAndArm(request);

  const before = await callSecretEcho(request, USER, "warmup");
  expect(before.isError).toBe(false);
  expect(before.text).toContain(`as=${USER}`);

  const stateBefore = await fakeState(request);

  // Wait past the access token's TTL. The SDK reads ``expires_at``
  // from local storage; once it's in the past, the next call should
  // swap to the refresh grant before sending. The SDK refreshes
  // proactively (it doesn't wait for the upstream's 401) — see
  // upstream_connection_service.py for the rationale.
  await new Promise((r) => setTimeout(r, POST_EXPIRY_SLEEP_MS));

  const after = await callSecretEcho(request, USER, "after-expiry");
  expect(after.isError).toBe(false);
  expect(after.text).toContain(`as=${USER}`);
  expect(after.text).toContain("after-expiry");

  // The strong signal: a refresh actually happened. Without this
  // delta the test would pass even if the gateway happened to
  // re-run a fresh OAuth handshake (which would be a regression of
  // the silent-refresh code path).
  const stateAfter = await fakeState(request);
  expect(stateAfter.refresh_grant_count).toBeGreaterThan(
    stateBefore.refresh_grant_count
  );
});
