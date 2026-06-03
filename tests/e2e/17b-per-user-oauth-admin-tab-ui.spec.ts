/**
 * UI-driven per_user_oauth take-over via the admin tab.
 *
 * Sibling spec to ``17-admin-oauth-takeover-ui``: same shape but
 * targeting ``oauth-tools-pu``. Phase B + Phase C make the
 * admin-tab UX uniform across both OAuth modes — popup
 * orchestration, "by <email>" slot-owner subtitle, and the plain
 * Disconnect → Authenticate take-over flow all render identically.
 */
import { test, expect, type APIRequestContext, type Page } from "@playwright/test";

import { apiLoginAs, loginAs, OAUTH_TEST_MCP_URL as FAKE_OAUTH, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN_A = "admin@example.com";
const ADMIN_B = "admin2@example.com";
const NON_ADMIN = "alice@example.com";
const UPSTREAM = "oauth-tools-pu";
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
    page.getByRole("heading", {
      name: "OAuth Tools (per-user)",
      exact: true,
    })
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
  await page.waitForTimeout(1500);
}

test.beforeEach(async ({ request }) => {
  await request.post(`${FAKE_OAUTH}/test/reset`);
  // Each potential row holder issues a per-user disconnect — covers
  // both admins and the non-admin so no leftover token leaks across
  // tests.
  for (const email of [ADMIN_A, ADMIN_B, NON_ADMIN]) {
    await apiLoginAs(request, email);
    await request.post(`${BACKEND}/api/auth/disconnect/${UPSTREAM}`);
  }
  await apiLoginAs(request, ADMIN_A);
  await request.post(
    `${BACKEND}/api/admin/upstreams/${UPSTREAM}/disconnect`
  );
});

test.describe("per_user_oauth take-over via the UI", () => {
  test("admin A clicks Authenticate, popup completes, slot owner is A", async ({
    page,
  }) => {
    await loginAs(page, ADMIN_A, ORG);
    await openUpstreamDetail(page);

    expect(await getSlotOwner(page.request)).toBeNull();
    await expect(
      page.getByRole("button", { name: /Authenticate/i })
    ).toBeVisible();

    await clickAndCompletePopup(page, /Authenticate/i, ADMIN_A);

    expect(await getSlotOwner(page.request)).toBe(ADMIN_A);

    // Bug 3.2 reproduction symmetric to spec 17: the button MUST
    // flip to Disconnect after the popup completes. The original
    // production bug was on per_user_oauth (Notion), so this is
    // the literal regression assertion.
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
    // Pre-seed the slot via the admin-tab API as admin A.
    await apiLoginAs(request, ADMIN_A);
    const connectResp = await request.post(
      `${BACKEND}/api/admin/upstreams/${UPSTREAM}/connect`
    );
    const body = await connectResp.json();
    if (!body.connected) {
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

    // Admin B logs in (separate browser context).
    const ctxB = await browser.newContext();
    const pageB = await ctxB.newPage();
    await loginAs(pageB, ADMIN_B, ORG);
    await openUpstreamDetail(pageB);

    // The status pill reads "Connected by <email>" when another
    // admin owns the slot. The action button is plain Disconnect.
    // Take-over is two clicks: Disconnect (clears A's row) then
    // Authenticate.
    await expect(
      pageB.getByText(`Ready, by ${ADMIN_A}`)
    ).toBeVisible({ timeout: 5_000 });
    await expect(
      pageB.getByRole("button", { name: /Disconnect/i })
    ).toBeVisible();

    await pageB.getByRole("button", { name: /Disconnect/i }).click();
    await expect(
      pageB.getByRole("button", { name: /Authenticate/i })
    ).toBeVisible({ timeout: 5_000 });
    expect(await getSlotOwner(pageB.request)).toBeNull();

    await clickAndCompletePopup(pageB, /Authenticate/i, ADMIN_B);

    expect(await getSlotOwner(pageB.request)).toBe(ADMIN_B);

    await ctxB.close();
  });
});
