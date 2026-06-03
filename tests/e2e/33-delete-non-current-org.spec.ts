import { test, expect } from "@playwright/test";
import { BACKEND_URL, createOrg, loginAs } from "./helpers";

/**
 * Deleting a non-current admin-org from /orgs/manage must:
 *   - remove the row from the table,
 *   - leave the current org unchanged (no cookie rotation, no
 *     navigation away from the manage page).
 *
 * Source: ``OrganizationsPage.tsx::handleDelete`` early-returns the
 * cookie-switch + navigation branch when ``wasCurrent === false``
 * and just calls ``reload()``. This spec guards that branch.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-del-${stamp}@test.com`;
const ORG_MAIN = `main-del-${stamp}`;
const ORG_EXTRA = `extra-del-${stamp}`;

test("deleting a non-current admin org keeps the current org selected", async ({
  page,
  request,
}) => {
  // Admin creates two orgs (admin in both).
  await createOrg(request, ADMIN, ORG_MAIN, "Main Org");
  await createOrg(request, ADMIN, ORG_EXTRA, "Extra Org");
  // Pin the cookie to ORG_MAIN so it's the "current" one.
  const switchResp = await request.post(
    `${BACKEND_URL}/api/orgs/${ORG_MAIN}/switch`,
  );
  expect(switchResp.status()).toBe(204);

  // Admin signs in via the page and lands on ORG_MAIN.
  await loginAs(page, ADMIN);
  // The page's request context inherits no cookies from `request`,
  // so explicitly switch its cookie to ORG_MAIN as well.
  const pageReq = page.context().request;
  const pageSwitch = await pageReq.post(
    `${BACKEND_URL}/api/orgs/${ORG_MAIN}/switch`,
  );
  expect(pageSwitch.status()).toBe(204);

  await page.goto("/orgs/manage");
  await expect(
    page.getByRole("heading", { name: /organizations/i })
  ).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("table").getByText("Main Org")).toBeVisible();
  await expect(page.locator("table").getByText("Extra Org")).toBeVisible();

  // Sanity: ORG_MAIN row carries the "Current" badge, ORG_EXTRA does
  // not.
  const mainRow = page.locator("tr", { hasText: "Main Org" });
  await expect(mainRow.getByText("Current")).toBeVisible();
  const extraRow = page.locator("tr", { hasText: "Extra Org" });
  await expect(extraRow.getByText("Current")).toHaveCount(0);

  // Delete ORG_EXTRA via its row's Trash2.
  await extraRow.getByTitle("Delete organization").click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText(/Type.*delete.*to confirm/),
  ).toBeVisible({ timeout: 5_000 });
  await dialog.getByRole("textbox").fill("delete");
  await dialog
    .getByRole("button", { name: "Delete organization" })
    .click();

  // The row is gone; we're still on the manage page.
  await expect(
    page.locator("table").getByText("Extra Org"),
  ).not.toBeVisible({ timeout: 5_000 });
  expect(new URL(page.url()).pathname).toBe("/orgs/manage");

  // ORG_MAIN still carries the "Current" badge.
  await expect(
    page.locator("tr", { hasText: "Main Org" }).getByText("Current"),
  ).toBeVisible();

  // /api/auth/me confirms the cookie was not rotated.
  const meResp = await pageReq.get(`${BACKEND_URL}/api/auth/me`);
  expect(meResp.status()).toBe(200);
  const me = (await meResp.json()) as {
    current_org: { slug: string } | null;
  };
  expect(me.current_org?.slug).toBe(ORG_MAIN);
});
