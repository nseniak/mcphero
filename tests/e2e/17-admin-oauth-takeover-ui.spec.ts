/**
 * UI-driven admin_oauth take-over flow.
 *
 * Sibling to ``15-admin-oauth-takeover.spec.ts`` (which drives the
 * API directly). This spec exercises the actual frontend code path
 * — popup window orchestration, "Sign in as me" button rendering,
 * SSE-driven post-OAuth refresh — to catch a class of bugs the API
 * tests can't see.
 *
 * Implementation notes:
 *
 * - State assertions go through the JSON API (``page.request.get``)
 *   rather than DOM selectors. The DOM is exercised for clicks,
 *   popup orchestration, and the "by <email>" subtitle rendered
 *   under the StatusBadge for non-owning admins.
 *
 * - The frontend's connect popup goes to whatever URL MCPolis
 *   returned, so the test can't append ``?email=`` to it. We
 *   ``POST /test/queue-email`` to the fake provider before each
 *   click — the next ``/authorize`` call without a query param
 *   pops from that queue.
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

import { apiLoginAs, loginAs, OAUTH_TEST_MCP_URL as FAKE_OAUTH, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN_A = "admin@example.com";
const ADMIN_B = "admin2@example.com";
const UPSTREAM = "oauth-tools";
async function queueOAuthEmail(page: Page, email: string) {
  const resp = await page.request.post(`${FAKE_OAUTH}/test/queue-email`, {
    form: { email },
  });
  expect(resp.status()).toBe(200);
}

async function getSlotOwner(
  api: APIRequestContext
): Promise<string | null> {
  const resp = await api.get(`${BACKEND}/api/admin/upstreams/${UPSTREAM}`);
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  return (body.slot_owner as string | null) ?? null;
}

async function openUpstreamDetail(page: Page) {
  await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM}`);
  await expect(
    page.getByRole("heading", { name: "OAuth Tools", exact: true })
  ).toBeVisible();
}

async function clickAndCompletePopup(
  page: Page,
  buttonName: RegExp,
  email: string
) {
  await queueOAuthEmail(page, email);
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: buttonName }).click();
  const popup = await popupPromise;
  await popup.waitForEvent("close", { timeout: 10_000 });
  // Grace for SSE-driven finalize callback before the next API
  // assertion. Frontend's useOAuthPopup polls + grace itself; this
  // matches.
  await page.waitForTimeout(1500);
}

// Reset the slot + fake-provider state before each test so the UI
// starts in the "no admin connected" state and a previous spec's
// queued email / TTL knob can't leak into this run.
test.beforeEach(async ({ request }) => {
  await request.post(`${FAKE_OAUTH}/test/reset`);
  await apiLoginAs(request, ADMIN_A);
  await request.post(
    `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
  );
});

test.describe("admin_oauth take-over via the UI", () => {
  test("admin A clicks Authenticate, popup completes, slot owner is A", async ({
    page,
  }) => {
    await loginAs(page, ADMIN_A, ORG);
    await openUpstreamDetail(page);

    // Pre-state: API confirms no owner; UI shows the OAuth-mode
    // "Authenticate" button.
    expect(await getSlotOwner(page.request)).toBeNull();
    await expect(
      page.getByRole("button", { name: /Authenticate/i })
    ).toBeVisible();

    await clickAndCompletePopup(page, /Authenticate/i, ADMIN_A);

    // Post-state: A owns the slot. Asserted via the same API the
    // ``15-admin-oauth-takeover`` spec uses — proves the
    // popup-driven flow ends in the same backend state as the
    // direct-POST flow.
    expect(await getSlotOwner(page.request)).toBe(ADMIN_A);

    // Bug 3.2 reproduction: after a successful Authenticate the
    // action button MUST flip to Disconnect. The original prod bug
    // had the button stay on "Authenticate" forever because the
    // ``connected`` field was computed against the wrong session
    // pool. Pinning the button-text transition catches any future
    // regression that mis-derives the visible action from the new
    // ``ready`` / ``slot_owner`` fields.
    await expect(
      page.getByRole("button", { name: /Disconnect/i })
    ).toBeVisible({ timeout: 5_000 });
    await expect(
      page.getByRole("button", { name: /Authenticate/i })
    ).not.toBeVisible();
  });

  test("admin B sees the slot-owner subtitle and takes over via Disconnect → Authenticate", async ({
    browser,
    request,
  }) => {
    // Pre-seed the slot via the API (faster than driving the UI for
    // setup) — the test under examination is the take-over UX, not
    // the initial-connect UX (covered by the test above).
    await apiLoginAs(request, ADMIN_A);
    const connectResp = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
    );
    const body = await connectResp.json();
    if (!body.connected) {
      // First connect → drive the OAuth flow ourselves to seed.
      await request.post(`${FAKE_OAUTH}/test/queue-email`, {
        form: { email: ADMIN_A },
      });
      const authorizeResp = await request.get(body.authorization_url, {
        maxRedirects: 0,
      });
      const callbackLoc = authorizeResp.headers()["location"];
      await request.get(callbackLoc, { maxRedirects: 0 });
    }
    expect(await getSlotOwner(request)).toBe(ADMIN_A);

    // Admin B logs in (separate browser context — real cookie
    // isolation, not just a re-login on the same page).
    const ctxB = await browser.newContext();
    const pageB = await ctxB.newPage();
    await loginAs(pageB, ADMIN_B, ORG);
    await openUpstreamDetail(pageB);

    // The status pill itself reads "Connected by <email>" when
    // another admin owns the slot. The action button is plain
    // Disconnect — no special take-over UX. Take-over is two
    // clicks: Disconnect (which clears A's row by design — see
    // admin_oauth disconnect semantics) then Authenticate.
    await expect(
      pageB.getByText(`Ready, by ${ADMIN_A}`)
    ).toBeVisible({ timeout: 5_000 });
    await expect(
      pageB.getByRole("button", { name: /Disconnect/i })
    ).toBeVisible();

    // Step 1: B clicks Disconnect — clears A's row.
    await pageB.getByRole("button", { name: /Disconnect/i }).click();
    await expect(
      pageB.getByRole("button", { name: /Authenticate/i })
    ).toBeVisible({ timeout: 5_000 });
    expect(await getSlotOwner(pageB.request)).toBeNull();

    // Step 2: B clicks Authenticate — claims the slot via OAuth.
    await clickAndCompletePopup(pageB, /Authenticate/i, ADMIN_B);

    expect(await getSlotOwner(pageB.request)).toBe(ADMIN_B);

    await ctxB.close();
  });
});
