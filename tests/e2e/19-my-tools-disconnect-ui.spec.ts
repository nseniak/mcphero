/**
 * /my-tools sign-out flow, driven through the actual browser.
 *
 * The user reported a production-only failure: signing out from a
 * per_user_oauth upstream on /my-tools left the card showing as
 * Signed In, even though the API-level test in
 * 16-per-user-oauth.spec.ts asserts the same flow at the
 * ``/api/user/mcps`` layer and passes. The gap was that nothing
 * exercised the browser-rendered surface — nothing caught a stale
 * cached fetch, an SSE-driven re-fetch racing the disconnect, or a
 * rendering bug that would never show up at the API layer.
 *
 * This spec walks the literal user steps:
 *   add upstream (seeded) -> authenticate via popup -> navigate to
 *   /my-tools -> click Disconnect -> assert the card is no longer
 *   "Signed In" (it should be "Unavailable", since per_user_oauth
 *   without a stored token can't do anything).
 */
import { test, expect, type Page } from "@playwright/test";

import { apiLoginAs, loginAs, OAUTH_TEST_MCP_URL as FAKE_OAUTH, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const USER = "admin@example.com";
const UPSTREAM = "oauth-tools-pu";
async function queueOAuthEmail(page: Page, email: string) {
  const resp = await page.request.post(`${FAKE_OAUTH}/test/queue-email`, {
    form: { email },
  });
  expect(resp.status()).toBe(200);
}

async function clickAndCompleteOAuthPopup(page: Page, email: string) {
  await queueOAuthEmail(page, email);
  const popupPromise = page.waitForEvent("popup");
  // ``Authenticate`` button on the upstream's admin detail page —
  // identical UX to the user-side connect button on /my-tools, but
  // the admin page was the route the user used to seed Mixpanel +
  // Notion, so we hit it the same way.
  await page.getByRole("button", { name: /Authenticate/i }).click();
  const popup = await popupPromise;
  await popup.waitForEvent("close", { timeout: 10_000 });
  await page.waitForTimeout(1_500);
}

test.beforeEach(async ({ request }) => {
  await request.post(`${FAKE_OAUTH}/test/reset`);
  await apiLoginAs(request, USER);
  // Per-user disconnect clears only this caller's row. After Phase B
  // unified the admin-tab UX across both OAuth modes, a leftover
  // row from another admin (e.g. admin2@example.com from specs 15b
  // / 17b) would put the page in the "Connected, by <other-admin>"
  // state — Disconnect button instead of Authenticate. So also
  // call the admin-tab disconnect to release any slot-owning
  // admin's row regardless of identity.
  await request.post(`${BACKEND}/api/auth/disconnect/${UPSTREAM}`);
  await request.post(
    `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
  );
});

test.describe("/my-tools sign-out (per_user_oauth)", () => {
  test("after Disconnect, the card is no longer rendered as Signed In", async ({
    page,
  }) => {
    await loginAs(page, USER, ORG);

    // Authenticate via the admin upstream detail page (the route the
    // user used to seed Mixpanel + Notion). This lands a per_user
    // token for USER on ``oauth-tools-pu``.
    await page.goto(`/orgs/${ORG}/admin/upstream/${UPSTREAM}`);
    await expect(
      page.getByRole("heading", { name: "OAuth Tools (per-user)", exact: true })
    ).toBeVisible();
    await clickAndCompleteOAuthPopup(page, USER);

    // Switch to /my-tools — the user-facing surface. Wait for the
    // initial fetch to land before any UI assertion.
    await page.goto(`/orgs/${ORG}/my-tools`);
    const card = page
      .locator("div", {
        has: page.getByRole("heading", { name: "OAuth Tools (per-user)", exact: true }),
      })
      .first();
    await expect(card).toBeVisible();

    // Pre-state: the row carries the "Signed in" status pill (from
    // ``StatusIndicator status="signed_in"``).
    await expect(card.getByText(/Signed in/i)).toBeVisible();

    // Click Disconnect — the rendered button text on the SignedInCard.
    // The card scopes the locator so we don't pick up some other
    // upstream's button.
    await card.getByRole("button", { name: /Disconnect/i }).click();

    // Post-state: after the in-flight POST + refetch settle, the
    // "Signed in" pill must disappear. The admin signing out is
    // also the only admin, so /my-tools sees ``ready=false`` →
    // the row falls into ``UnavailableCard``. The strong assertion
    // is "Signed in is no longer visible" — that's what would have
    // failed in production. ``Unavailable`` should also show up but
    // we keep the assertion narrow to the regression's surface.
    await expect(card.getByText(/Signed in/i)).not.toBeVisible({
      timeout: 5_000,
    });
  });
});
