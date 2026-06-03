import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

test.describe("Auth & Onboarding", () => {
  test("unauthenticated user sees login page", async ({ page }) => {
    await page.goto("/");
    // Should redirect to login or show auth prompt
    await expect(
      page.getByRole("link", { name: /sign in/i }).or(
        page.getByRole("button", { name: /sign in/i })
      )
    ).toBeVisible({ timeout: 10_000 });
  });

  test("new user with no orgs sees signup page", async ({ page }) => {
    await loginAs(page, "newuser@test.com", "");
    await page.goto("/");
    await expect(page.getByText("Create your organization")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("admin user lands on upstream MCPs page", async ({ page }) => {
    await loginAs(page, "admin@example.com", "acme-corp");
    await page.goto("/");
    await page.waitForURL(/\/orgs\/acme-corp\/admin\/upstream/);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();
  });

  test("sidebar links use org slug prefix", async ({ page }) => {
    await loginAs(page, "admin@example.com", "acme-corp");
    await page.goto("/orgs/acme-corp/admin/upstream");
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();

    const teamLink = page.getByRole("link", { name: "Team" });
    await expect(teamLink).toHaveAttribute(
      "href",
      "/orgs/acme-corp/admin/team"
    );
  });
});
