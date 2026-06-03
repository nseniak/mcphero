import { test, expect } from "@playwright/test";
import { loginAs, createOrg } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

test.describe("Organizations Page", () => {
  test("manage organizations page loads", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto("/orgs/manage");
    await expect(page.getByRole("heading", { name: /organizations/i })).toBeVisible();
  });

  test("shows current org in list", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto("/orgs/manage");
    // The org name appears in the table
    await expect(page.locator("table").getByText("Acme Corp")).toBeVisible();
  });

  test("admin sees delete button for their org", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto("/orgs/manage");
    // Wait for the org list to load (async fetch), then check delete button
    await expect(page.locator("table").getByText("Acme Corp")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTitle("Delete organization")).toBeVisible();
  });

  test("non-admin does not see delete button", async ({ page }) => {
    await loginAs(page, "alice@example.com", ORG);
    await page.goto("/orgs/manage");
    // Non-admin should see the page and their org
    await expect(page.getByRole("heading", { name: /organizations/i })).toBeVisible({ timeout: 10_000 });
    // The delete button should not be present (only shown for admins)
    await expect(page.getByTitle("Delete organization")).toHaveCount(0);
  });

  test("create new org form toggles", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto("/orgs/manage");
    // Click "New organization" button
    await page.getByRole("button", { name: /new organization/i }).click();
    await expect(page.getByPlaceholder("Acme Corp")).toBeVisible();
    // Click "Cancel" to close
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByPlaceholder("Acme Corp")).not.toBeVisible();
  });

  test("org switcher has manage link", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByRole("link", { name: "Manage organizations" })).toBeVisible();
  });

  test("can create and delete an org", async ({ page, request }) => {
    // Create a temporary org via API
    await createOrg(request, ADMIN, "temp-org", "Temp Org");
    // Login with the new org visible
    await loginAs(page, ADMIN, "acme-corp");
    await page.goto("/orgs/manage");
    await expect(page.locator("table").getByText("Temp Org")).toBeVisible({ timeout: 10_000 });

    // Click delete on the temp org
    const row = page.locator("tr", { hasText: "Temp Org" });
    await row.getByTitle("Delete organization").click();

    // Dialog should appear with text input requirement
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText(/Type.*delete.*to confirm/)).toBeVisible({ timeout: 5_000 });

    // Button should be disabled until text is typed
    const confirmBtn = dialog.getByRole("button", { name: "Delete organization" });
    await expect(confirmBtn).toBeDisabled();

    // Type "delete" to enable the button
    await dialog.getByRole("textbox").fill("delete");
    await expect(confirmBtn).toBeEnabled();

    // Confirm deletion
    await confirmBtn.click();

    // Temp Org should be gone from the table
    await expect(page.locator("table").getByText("Temp Org")).not.toBeVisible({ timeout: 5_000 });
  });

  test("navigating to non-existing org shows error", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto("/orgs/does-not-exist/admin/upstream");
    await expect(page.getByText("Organization not found")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("Go to your dashboard")).toBeVisible();
  });
});
