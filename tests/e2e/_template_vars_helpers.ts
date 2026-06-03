/**
 * Shared helpers for the 20*-template-vars.spec.ts split.
 */
import { expect, type Page } from "@playwright/test";
export { loginAs, TEST_MCP_URL } from "./helpers";
import { TEST_MCP_URL as _TEST_MCP_URL } from "./helpers";
// Local alias so the inline ``${TEST_MCP_URL}`` references compile
// against the helpers in this module (the re-export above is for the
// spec files; this alias is for the helpers themselves).
const TEST_MCP_URL = _TEST_MCP_URL;
void TEST_MCP_URL;
export const ORG = "acme-corp";
export const ADMIN = "admin@example.com";

/**
 * Build a unique upstream id per test so concurrent runs don't
 * collide on the seeded ``test-tools`` upstream and the cascade
 * delete on tear-down doesn't wipe each other's data.
 */
export function uniqueId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

export async function openAddForm(page: Page): Promise<void> {
  await page.goto(`/orgs/${ORG}/admin/upstream`);
  await expect(
    page.getByRole("heading", { name: "Upstream MCPs" }),
  ).toBeVisible({ timeout: 10_000 });
  // Open the wizard.
  await page.getByRole("button", { name: /^Add MCP/ }).click();
  // Switch to JSON tab.
  await page.getByRole("button", { name: /JSON/ }).click();
}

export async function fillJsonAndAdvance(
  page: Page,
  upstreamId: string,
  json: string,
): Promise<void> {
  // The JSON box is the first textarea in the wizard.
  await page.locator("textarea").first().fill(json);
  await page.getByRole("button", { name: /^Next/ }).click();
  // The wizard auto-fills the form ID from the wrapped JSON key, so
  // we don't need to type into the ID field directly (which doesn't
  // associate its label via htmlFor). Override via an exact-match
  // placeholder selector — both the ID and display-name fields have
  // "e.g." prefixes, so we have to scope tightly.
  const idInput = page.getByPlaceholder("e.g. slack", { exact: true });
  await idInput.fill(upstreamId);
}
