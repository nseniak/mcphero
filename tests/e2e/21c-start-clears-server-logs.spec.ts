/**
 * Start-clears — UI: ``<pre>`` log accumulator never holds more than
 * one SLOW-MARKER per attempt. Proves the ``logsResetEpoch`` bump
 * inside ``onOptimisticReset`` actually drops the previous session's
 * stderr from the in-memory React buffer.
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

test("Server logs reset on Start", async ({ page, request }) => {
  const api = await adminApi(request);
  await loginAs(page, ADMIN, ORG);

  // 5s sleep before exit holds ``starting=true`` long enough for SSE
  // to deliver the new attempt's marker before the failure-driven
  // reload, so the count is observable as exactly one.
  await api.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: UPSTREAM_ID,
      display_name: "Bogus stdio (e2e 21 logs)",
      command: "python3",
      args: [
        "-c",
        "import sys, time; sys.stderr.write('SLOW-MARKER\\n'); time.sleep(5); sys.exit(1)",
      ],
      auth_mode: "service_account",
    },
  });
  await api.post(`${BACKEND_URL}/api/admin/upstreams/${UPSTREAM_ID}/reconnect`);
  await waitForCondition(async () => {
    const d = await fetchDetail(api, UPSTREAM_ID);
    return d.disconnect_reason !== null && d.starting === false;
  }, 12_000, 200);

  await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM_ID}`);
  // Execution logs is foldable inside the Sandbox card — expand it
  // so the SSE subscriber attaches and the <pre> renders.
  await page.getByRole("button", { name: /Execution logs/i }).click();
  const logsPre = page.locator("pre").first();
  await expect(logsPre).toContainText(/SLOW-MARKER/, { timeout: 8_000 });

  await page.getByRole("button", { name: /^Start$/ }).click();

  let observedCount = 0;
  await waitForCondition(async () => {
    const text = await page
      .locator("pre")
      .first()
      .innerText()
      .catch(() => "");
    observedCount = text.split("SLOW-MARKER").length - 1;
    return observedCount >= 1;
  }, 10_000, 100);
  expect(
    observedCount,
    "logs panel should hold exactly one SLOW-MARKER (the new attempt's). Two means the previous session's stderr leaked through.",
  ).toBeLessThanOrEqual(1);
});
