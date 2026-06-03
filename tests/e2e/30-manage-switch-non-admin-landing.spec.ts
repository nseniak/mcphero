import { test, expect } from "@playwright/test";
import { BACKEND_URL, createOrg, loginAs } from "./helpers";

/**
 * Sibling of 29-org-switcher-non-admin-landing.spec.ts. Same bug,
 * different surface: the ``Switch`` button on the /orgs/manage table
 * (``OrganizationsPage.tsx::handleSwitch``) hard-navigates to
 * ``/orgs/${slug}/admin/upstream`` regardless of the user's role in
 * the target org, so a non-admin lands on the admin page and
 * ``UpstreamsPage`` 403s on ``GET /api/admin/upstreams``.
 */

const stamp = Date.now().toString(36);
const ADMIN = `admin-mng-${stamp}@test.com`;
const USER = `user-mng-${stamp}@test.com`;
const ORG1 = `org1-mng-${stamp}`;
const ORG2 = `org2-mng-${stamp}`;

test("Manage-page Switch must not land a non-admin user on /admin/upstream", async ({
  page,
  request,
}) => {
  // Admin sets up org1 and pre-approves user@ as a non-admin there.
  await createOrg(request, ADMIN, ORG1, "Org One");
  let resp = await request.post(`${BACKEND_URL}/api/orgs/${ORG1}/switch`);
  expect(resp.status()).toBe(204);
  resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: USER, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());

  // User signs in (membership materializes) and creates org2 (admin
  // there). Their cookie is then pointed at org2.
  await loginAs(page, USER);
  const pageReq = page.context().request;
  resp = await pageReq.post(`${BACKEND_URL}/api/orgs`, {
    data: { slug: ORG2, display_name: "Org Two", created_via: "manage_page" },
  });
  expect([200, 201]).toContain(resp.status());
  resp = await pageReq.post(`${BACKEND_URL}/api/orgs/${ORG2}/switch`);
  expect(resp.status()).toBe(204);

  // Open the manage page (cookie still on org2; user is admin here).
  await page.goto("/orgs/manage");
  await expect(
    page.getByRole("heading", { name: /organizations/i })
  ).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("table").getByText("Org One")).toBeVisible();

  // Listen for any /api/admin/upstreams response so we can prove no
  // 403 was raised by an unintended admin-page render.
  const adminResponses: Array<{ status: number; body: string }> = [];
  page.on("response", async (r) => {
    if (new URL(r.url()).pathname === "/api/admin/upstreams") {
      adminResponses.push({
        status: r.status(),
        body: await r.text().catch(() => ""),
      });
    }
  });

  // Click the Switch button on the org1 (non-admin) row.
  const row = page.locator("tr", { hasText: "Org One" });
  await row.getByRole("button", { name: /switch/i }).click();

  await page.waitForURL(`**/orgs/${ORG1}/**`, { timeout: 10_000 });
  await page.waitForTimeout(500);

  const landedPath = new URL(page.url()).pathname;
  expect(
    landedPath,
    `Manage-page Switch routed a non-admin to ${landedPath}; expected /my-tools or /connect.`
  ).not.toContain("/admin/");

  const offending = adminResponses.find((r) => r.status === 403);
  expect(
    offending,
    `Switching to a non-admin org via the manage page triggered ` +
      `${offending?.status} on /api/admin/upstreams with body ` +
      `"${offending?.body}". The Switch button must not navigate to ` +
      `/admin/* for non-admins.`
  ).toBeUndefined();
});
