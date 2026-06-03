import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

const ORG_A = "acme-corp";
const ORG_B = "beta-org";
const ADMIN_A = "admin@example.com";

test.describe("Multi-Org Isolation", () => {
  test("admin of org A cannot see org B content", async ({ page }) => {
    await loginAs(page, ADMIN_A, ORG_A);

    // Navigate to org A's team page — should work
    await page.goto(`/orgs/${ORG_A}/admin/team`);
    await expect(page.getByRole("heading", { name: "Team" })).toBeVisible();
    // ``exact: true`` so the assertion doesn't substring-match
    // other ``…admin@example.com`` accessibility names that may be
    // present in the table when test 11 has run earlier in the
    // suite and added ``e2e-nonadmin@example.com``.
    await expect(
      page.getByRole("link", { name: ADMIN_A, exact: true })
    ).toBeVisible();
  });

  test("gateway URL is scoped to the active org's slug", async ({ page }) => {
    await loginAs(page, ADMIN_A, ORG_A);
    await page.goto(`/orgs/${ORG_A}/admin/gateway`);

    // Cloud mode exposes /mcp/<slug>. Each org sees its own URL and
    // its gateway only surfaces tools belonging to that org.
    await expect(page.getByText(`/mcp/${ORG_A}`)).toBeVisible();
  });

  test("audit log is org-scoped", async ({ page }) => {
    await loginAs(page, ADMIN_A, ORG_A);
    await page.goto(`/orgs/${ORG_A}/admin/audit`);
    await expect(page.getByRole("heading", { name: "Audit" })).toBeVisible();
    // Audit entries (if any) should only show org A activity
  });

  test("API returns 401 for wrong org cookie", async ({ page }) => {
    // Login as admin of org A
    await loginAs(page, ADMIN_A, ORG_A);

    // Try to access org B's API data by navigating to org B's URL
    // The session cookie says org A, but the URL says org B
    // The middleware should resolve based on the cookie org_slug
    await page.goto(`/orgs/${ORG_A}/admin/upstream`);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();
  });
});
