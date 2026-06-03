import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

test.describe("Upstream MCPs with test tools", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("upstream list shows test-tools", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });
  });

  test("upstream detail shows discovered tools", async ({ page }) => {
    // Navigate from the list to ensure the page loads correctly
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });
    // Click through to detail
    await page.getByRole("link", { name: "Test Tools" }).click();
    await page.waitForURL(/upstream\/test-tools/);
    // Tools may take a moment to be discovered after connection
    await expect(page.getByText("echo", { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("add", { exact: true })).toBeVisible();
    await expect(page.getByText("greet", { exact: true })).toBeVisible();
  });
});

test.describe("Roles & Permissions with test tools", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    // Navigate via sidebar to warm up auth
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();
    await page.getByRole("link", { name: "Roles & Permissions" }).click();
    await expect(page).toHaveURL(/permissions/);
  });

  test("shows role tabs", async ({ page }) => {
    // Role tabs should be visible
    await expect(page.getByText("MCP Permissions")).toBeVisible({ timeout: 10_000 });
  });

  test("shows MCP access toggle for test-tools", async ({ page }) => {
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });
  });

  test("shows team member count link", async ({ page }) => {
    await expect(page.getByText(/team member/)).toBeVisible({ timeout: 10_000 });
  });

  test("team member link navigates to team page with role filter", async ({ page }) => {
    await page.getByText(/team member/).first().click();
    await expect(page).toHaveURL(/admin\/team/);
  });
});

test.describe("Audit log", () => {
  test("audit page loads from sidebar", async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();
    await page.getByRole("link", { name: "Audit" }).click();
    await expect(page).toHaveURL(/audit/);
  });
});
