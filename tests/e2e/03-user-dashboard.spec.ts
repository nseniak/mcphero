import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

const ORG = "acme-corp";
const USER = "alice@example.com";

test.describe("User Dashboard", () => {
  test("non-admin user sees connect page", async ({ page }) => {
    await loginAs(page, USER, ORG);
    await page.goto(`/orgs/${ORG}/connect`);
    await expect(page.locator("main")).toContainText(/connect|gateway/i, {
      timeout: 10_000,
    });
  });

  test("non-admin user sees my-tools page", async ({ page }) => {
    await loginAs(page, USER, ORG);
    await page.goto(`/orgs/${ORG}/my-tools`);
    await expect(
      page.getByRole("heading", { name: "My Tools" })
    ).toBeVisible({ timeout: 10_000 });
  });

  test("non-admin user does not see admin sidebar links", async ({ page }) => {
    await loginAs(page, USER, ORG);
    await page.goto(`/orgs/${ORG}/my-tools`);
    await expect(
      page.getByRole("heading", { name: "My Tools" })
    ).toBeVisible();
    // The per-org Admin section header should not render. Use
    // ``exact: true`` so the assertion doesn't substring-match the
    // "Superadmin" section header (which DOES render when the test
    // user is in MCPOLIS_SUPERADMIN_EMAILS — a superadmin can be a
    // non-org-admin in any given org and still see cross-org
    // superadmin links; the test's intent is the org-scoped
    // "Admin" block, not the global one).
    await expect(
      page.getByRole("complementary").getByText("Admin", { exact: true })
    ).not.toBeVisible();
  });
});
