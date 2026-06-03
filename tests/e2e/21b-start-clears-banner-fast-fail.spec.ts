/**
 * Start-clears — UI: the red banner unmounts within 1s of the Start
 * click, even when the new attempt fails in ~1s (the case the
 * frontend's ``onOptimisticReset`` was added to fix).
 *
 * Split out of the historic 21-start-clears-stale-error-and-logs.spec.ts.
 */
import { test, expect } from "@playwright/test";

import {
  ORG,
  ADMIN,
  UPSTREAM_ID,
  adminApi,
  fetchDetail,
  waitForCondition,
  loginAs,
  BACKEND_URL,
} from "./_start_clears_helpers";

test.describe.configure({ timeout: 60_000 });

test.beforeEach(async ({ request }) => {
  const api = await adminApi(request);
  await api.delete(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}`);
});

test.afterEach(async ({ request }) => {
  const api = await adminApi(request);
  await api.delete(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}`);
});

test("UI banner hides on Start (fast-fail command)", async ({
  page,
  request,
}) => {
  const api = await adminApi(request);
  await loginAs(page, ADMIN, ORG);

  await api.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: UPSTREAM_ID,
      display_name: "Bogus stdio (e2e 21 UI)",
      command: "python3",
      args: ["this-script-does-not-exist"],
      auth_mode: "service_account",
    },
  });

  await api.post(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`);
  await waitForCondition(async () => {
    const d = await fetchDetail(api, UPSTREAM_ID);
    return d.disconnect_reason !== null && d.starting === false;
  }, 8_000, 100);

  await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM_ID}`);
  const errorText = page.getByText(/Process exited with code/i).first();
  await expect(errorText).toBeVisible({ timeout: 10_000 });

  // Optimistic reset fires synchronously before the API roundtrip + the
  // 1s MIN_DELAY; banner must clear within 1s even when the new failure
  // re-appears at ~1.5s.
  await page.getByRole("button", { name: /^Start$/ }).click();
  await expect(
    page.getByText(/Process exited with code/i),
  ).toHaveCount(0, { timeout: 1_000 });
});
