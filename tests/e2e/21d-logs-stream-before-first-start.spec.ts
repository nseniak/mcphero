/**
 * Regression: Execution logs must (a) be foldable, and (b) stream
 * live in the *first* render — even when the operator opens the
 * detail page (and the SSE) BEFORE the stdio MCP has ever started.
 *
 * Bug history: when the LogViewer was relocated into the Sandbox card
 * (5c96bc2) the chevron-style fold was removed and the SSE became
 * always-on at page mount. For an unstarted MCP the gateway has no
 * ``LogBuffer`` yet, so ``GET /upstreams/{id}/logs/stream`` returned
 * 404. EventSource auto-reconnect only fires for connection-drops
 * after a 200 OK handshake; a non-200 response puts it straight into
 * CLOSED with no retry, so the operator saw nothing until they
 * refreshed the page (which created a fresh EventSource against a
 * by-now-existing buffer).
 *
 * Fix is two-sided:
 *   - Frontend: restore the chevron fold so the panel can be
 *     collapsed AND the EventSource only opens when expanded.
 *   - Backend: SSE handler creates the buffer eagerly via
 *     ``get_or_create`` for stdio upstreams instead of 404'ing,
 *     so the subscriber sits idle until the MCP eventually writes
 *     and the bytes flow without a refresh.
 *
 * This spec exercises both: the chevron must exist (foldable) and
 * expanding it BEFORE clicking Start must still surface stderr live.
 */
import { test, expect } from "@playwright/test";

import {
  ORG,
  ADMIN,
  UPSTREAM_ID,
  adminApi,
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

test("Execution logs fold + stream live when SSE opens before first Start", async ({
  page,
  request,
}) => {
  const api = await adminApi(request);
  await loginAs(page, ADMIN, ORG);

  // Create a never-yet-started stdio upstream. The 5s sleep keeps
  // ``starting=true`` long enough for an SSE chunk to arrive while
  // we're still on the same page render.
  await api.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: UPSTREAM_ID,
      display_name: "Bogus stdio (e2e 21d before-start stream)",
      command: "python3",
      args: [
        "-c",
        "import sys, time; sys.stderr.write('FIRST-START-MARKER\\n'); time.sleep(5); sys.exit(1)",
      ],
      auth_mode: "service_account",
    },
  });

  await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM_ID}`);

  // Foldable contract: the Execution logs row exposes a chevron
  // toggle. Default state is collapsed — the SSE only opens once
  // the operator expands. Locate by accessible name so the test
  // doesn't couple to icon markup.
  const logsToggle = page.getByRole("button", { name: /Execution logs/i });
  await expect(logsToggle).toBeVisible();
  await expect(logsToggle).toHaveAttribute("aria-expanded", "false");

  // Other cards on the page (e.g. JSON configuration editor) may
  // render their own ``<pre>`` blocks, so scope the log locator to
  // the row that follows the Execution logs toggle. The fold body
  // is the toggle's next sibling div.
  const logsBody = logsToggle.locator(
    "xpath=following-sibling::div[1]",
  );

  // Expand BEFORE clicking Start. This is the smoking-gun ordering:
  // SSE opens against an upstream that has never had a session, so
  // the gateway has no LogBuffer yet. With the backend fix the
  // handler creates one lazily and the subscriber sits idle; without
  // it the response is 404 and the EventSource closes for good.
  await logsToggle.click();
  await expect(logsToggle).toHaveAttribute("aria-expanded", "true");

  // Now click Start.
  await page.getByRole("button", { name: /^Start$/ }).click();

  // The MCP's stderr marker must reach the live SSE-driven <pre>
  // — no page refresh, no second navigation, no second expand.
  await expect(logsBody.locator("pre")).toContainText(
    /FIRST-START-MARKER/,
    { timeout: 10_000 },
  );

  // And the fold still works: collapsing tears the <pre> down.
  await logsToggle.click();
  await expect(logsToggle).toHaveAttribute("aria-expanded", "false");
  await expect(logsBody.locator("pre")).toHaveCount(0);
});
