import { test, expect } from "@playwright/test";
import { BACKEND_URL, createOrg, loginAs } from "./helpers";

/**
 * The /orgs/manage table renders a Trash2 (``title="Delete
 * organization"``) only on rows where the user's role is admin
 * (``OrganizationsPage.tsx`` line ~333: ``{isAdmin && (<button …>)}``).
 *
 * The existing 07-organizations.spec.ts covers the all-non-admin
 * case (no rows have a delete button). This spec exercises the
 * mixed case: a user who is admin in one org and a plain user in
 * another should see the button only on the admin row.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-vis-${stamp}@test.com`;
const USER = `user-vis-${stamp}@test.com`;
const ORG_USER = `userorg-vis-${stamp}`;
const ORG_OWN = `ownorg-vis-${stamp}`;

test("manage-page delete button visible only on admin rows", async ({
  page,
  request,
}) => {
  // Admin creates ORG_USER, pre-approves user@ as ``user`` there.
  await createOrg(request, ADMIN, ORG_USER, "Member Org");
  let resp = await request.post(`${BACKEND_URL}/api/orgs/${ORG_USER}/switch`);
  expect(resp.status()).toBe(204);
  resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: USER, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());

  // User signs in (membership materializes) and creates ORG_OWN
  // (admin there).
  await loginAs(page, USER);
  const pageReq = page.context().request;
  resp = await pageReq.post(`${BACKEND_URL}/api/orgs`, {
    data: {
      slug: ORG_OWN,
      display_name: "User-Owned Org",
      created_via: "manage_page",
    },
  });
  expect([200, 201]).toContain(resp.status());

  await page.goto("/orgs/manage");
  await expect(
    page.getByRole("heading", { name: /organizations/i })
  ).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("table").getByText("Member Org")).toBeVisible();
  await expect(page.locator("table").getByText("User-Owned Org")).toBeVisible();

  // Admin row → delete button present.
  const ownRow = page.locator("tr", { hasText: "User-Owned Org" });
  await expect(ownRow.getByTitle("Delete organization")).toBeVisible();

  // Non-admin row → no delete button at all.
  const memberRow = page.locator("tr", { hasText: "Member Org" });
  await expect(memberRow.getByTitle("Delete organization")).toHaveCount(0);
});
