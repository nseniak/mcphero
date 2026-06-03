import { test, expect } from "@playwright/test";
import { loginAs } from "./helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";

test.describe("Admin Dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Upstream MCPs page loads", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible({ timeout: 10_000 });
  });

  test("Gateway MCP page shows the slug-scoped /mcp/<slug> URL", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/gateway`);
    await expect(page.getByRole("heading", { name: "Gateway MCP" })).toBeVisible();
    // Cloud mode surfaces the slug-scoped URL so each org's gateway
    // surfaces only that org's tools (the bare /mcp merge URL still
    // works under the hood but is intentionally not exposed).
    await expect(page.getByText(`/mcp/${ORG}`)).toBeVisible();
  });

  test("Team page loads and shows invite link", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/team`);
    await expect(page.getByRole("heading", { name: "Team" })).toBeVisible();
    // Invite link should use /orgs/ prefix
    await expect(page.getByText(`/orgs/${ORG}/join`)).toBeVisible();
  });

  test("Team page shows members", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/team`);
    // Use a specific locator — the email appears in sidebar, header, AND table
    await expect(page.getByRole("link", { name: ADMIN, exact: true })).toBeVisible();
  });

  test("Clicking a team member navigates to detail", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/team`);
    await expect(page.getByRole("link", { name: ADMIN, exact: true })).toBeVisible();
    await page.getByRole("link", { name: ADMIN, exact: true }).click();
    await page.waitForURL(/\/orgs\/acme-corp\/admin\/team\//);
    await expect(page.getByRole("heading").filter({ hasText: ADMIN })).toBeVisible();
  });

  test("Roles & Permissions page loads", async ({ page }) => {
    // Navigate to upstream page first (warm up auth), then click sidebar
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await expect(page.getByRole("heading", { name: "Upstream MCPs" })).toBeVisible();
    await page.getByRole("link", { name: "Roles & Permissions" }).click();
    await expect(page).toHaveURL(/permissions/);
  });

  test("Audit page loads", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/audit`);
    await expect(page.getByRole("link", { name: "Audit" })).toHaveAttribute(
      "aria-current", "page"
    );
  });

  test("Admin MCP page loads", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/admin-mcp`);
    await expect(page.getByRole("link", { name: "Admin MCP" })).toHaveAttribute(
      "aria-current", "page"
    );
    // Should show the admin MCP URL somewhere in main content
    await expect(page.locator("main")).toContainText("admin-mcp");
  });

  test("navigation between pages preserves slug", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    await page.getByRole("link", { name: "Team" }).click();
    await expect(page).toHaveURL(/\/orgs\/acme-corp\/admin\/team/);
    await page.getByRole("link", { name: "Audit" }).click();
    await expect(page).toHaveURL(/\/orgs\/acme-corp\/admin\/audit/);
    await page.getByRole("link", { name: "Upstream MCPs" }).click();
    await expect(page).toHaveURL(/\/orgs\/acme-corp\/admin\/upstream/);
  });
});
