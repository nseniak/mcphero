/**
 * Start-clears — API: ``disconnect_reason`` flips to ``null`` while
 * ``starting=true`` immediately after a Start click. Pins the backend
 * precondition so the optimistic UI in
 * ``useUpstreamActions.onOptimisticReset`` has something correct to
 * converge to.
 *
 * Split out of the historic 21-start-clears-stale-error-and-logs.spec.ts
 * so this branch can land on its own shard. See
 * ``_start_clears_helpers.ts`` for shared fixtures.
 */
import { test, expect } from "@playwright/test";

import {
  ORG,
  UPSTREAM_ID,
  adminApi,
  fetchDetail,
  waitForCondition,
  BACKEND_URL,
} from "./_start_clears_helpers";

void ORG;

test.describe.configure({ timeout: 60_000 });

test.beforeEach(async ({ request }) => {
  const api = await adminApi(request);
  await api.delete(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}`);
});

test.afterEach(async ({ request }) => {
  const api = await adminApi(request);
  await api.delete(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}`);
});

test("clicking Start clears the previous disconnect_reason immediately", async ({
  request,
}) => {
  const api = await adminApi(request);

  const createResp = await api.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: UPSTREAM_ID,
      display_name: "Bogus stdio (e2e 21)",
      command: "python3",
      args: ["this-script-does-not-exist"],
      auth_mode: "service_account",
    },
  });
  expect([200, 201]).toContain(createResp.status());

  const reconnect1 = await api.post(
    `${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`,
  );
  expect(reconnect1.status()).toBe(200);

  const firstFailureSeen = await waitForCondition(async () => {
    const d = await fetchDetail(api, UPSTREAM_ID);
    return d.disconnect_reason !== null && d.starting === false;
  }, 10_000, 100);
  expect(firstFailureSeen, "first connect should have failed by now").toBe(true);

  const beforeReconnect = await fetchDetail(api, UPSTREAM_ID);
  expect(beforeReconnect.disconnect_reason).not.toBeNull();
  const firstError = beforeReconnect.disconnect_reason!;
  expect(firstError).toMatch(/Process exited with code|completing MCP handshake|exit/i);

  const reconnect2 = await api.post(
    `${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`,
  );
  expect(reconnect2.status()).toBe(200);

  // Race: poll quickly to catch the cleared-while-starting window
  // before the new attempt's fail-fast (~500ms) overwrites the reason.
  let sawClearedWhileStarting = false;
  const startedRace = Date.now();
  while (Date.now() - startedRace < 5_000) {
    const d = await fetchDetail(api, UPSTREAM_ID);
    if (d.starting && d.disconnect_reason === null) {
      sawClearedWhileStarting = true;
      break;
    }
    if (!d.starting && d.disconnect_reason !== null) {
      break;
    }
    await new Promise((r) => setTimeout(r, 25));
  }
  expect(
    sawClearedWhileStarting,
    "disconnect_reason must be cleared while starting=true (before the new attempt completes)",
  ).toBe(true);

  const secondFailureSeen = await waitForCondition(async () => {
    const d = await fetchDetail(api, UPSTREAM_ID);
    return d.disconnect_reason !== null && d.starting === false;
  }, 10_000, 100);
  expect(secondFailureSeen, "second connect should also have failed").toBe(true);
});
