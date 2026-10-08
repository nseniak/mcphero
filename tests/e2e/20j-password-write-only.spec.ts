/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Passwords are write-only".
 *
 * A saved password (``is_secret=true`` Variable) never leaves the
 * backend again: the list API and the page carry only "set" /
 * "empty". Edit with a blank value keeps it, Clear empties it, and a
 * rename moves the saved value server-side.
 *
 * Each describe lives in its own spec file so the
 * orchestrator (tests/run-e2e-tests.py) can spread them
 * across shards. Shared helpers in
 * ``_template_vars_helpers.ts``.
 */
import { test, expect, type Page } from "@playwright/test";

import {
  loginAs,
  TEST_MCP_URL,
  ORG,
  ADMIN,
  uniqueId,
} from "./_template_vars_helpers";

interface VarRow {
  name: string;
  is_secret: boolean;
  value: string | null;
  has_value: boolean;
  updated_at: string;
}

/** Seed an HTTP upstream (no sandbox spawn) with one password. */
async function seedUpstreamWithPassword(
  page: Page, upstreamId: string, varName: string, secret: string,
): Promise<void> {
  await page.goto(`/orgs/${ORG}/admin/upstream`);
  await expect(
    page.getByRole("heading", { name: "Upstream MCPs" }),
  ).toBeVisible({ timeout: 10_000 });
  await page.evaluate(
    async ({ upstreamId, varName, secret, testMcpUrl }) => {
      const r = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "Write-only Test",
          url: `${testMcpUrl}/mcp`,
          auth_mode: "service_account",
        }),
      });
      if (!r.ok) {
        throw new Error(`add upstream failed: ${r.status} ${await r.text()}`);
      }
      const r2 = await fetch(
        `/api/admin/upstreams/${upstreamId}/template-vars/${varName}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: secret, is_secret: true }),
        },
      );
      if (!r2.ok) {
        throw new Error(`set var failed: ${r2.status} ${await r2.text()}`);
      }
    },
    { upstreamId, varName, secret, testMcpUrl: TEST_MCP_URL },
  );
}

/** The raw list response text, as any API client would receive it. */
async function fetchVarsText(page: Page, upstreamId: string): Promise<string> {
  return page.evaluate(async (id: string) => {
    const r = await fetch(`/api/admin/upstreams/${id}/template-vars`);
    return r.text();
  }, upstreamId);
}

async function fetchVars(page: Page, upstreamId: string): Promise<VarRow[]> {
  return JSON.parse(await fetchVarsText(page, upstreamId)) as VarRow[];
}

test.describe("Passwords are write-only", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("a saved password is never sent back, only 'set'", async ({ page }) => {
    const id = uniqueId("wonly");
    const SECRET = "ghp_writeonlytestlongvalue1234abcd";
    await seedUpstreamWithPassword(page, id, "SECRET_VAR", SECRET);

    const text = await fetchVarsText(page, id);
    expect(text).not.toContain(SECRET);
    const [row] = JSON.parse(text) as VarRow[];
    expect(row).toMatchObject({
      name: "SECRET_VAR", is_secret: true, value: null, has_value: true,
    });

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(page.getByText("SECRET_VAR")).toBeVisible();
    await expect(page.getByText("•••• set")).toBeVisible();
    await expect(page.getByLabel(/Reveal value/i)).toHaveCount(0);
    expect(await page.content()).not.toContain(SECRET);
  });

  test("Edit with a blank value keeps the saved password", async ({ page }) => {
    const id = uniqueId("keep");
    await seedUpstreamWithPassword(page, id, "KEEP_ME", "keep-value-1234567890");
    const [before] = await fetchVars(page, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByTitle(/Replace value/).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Edit KEEP_ME/ });
    await expect(
      dialog.getByPlaceholder(/Leave blank to keep the saved value/),
    ).toHaveValue("");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Save another change in the same SETTINGS Save, so the request
    // really goes out with the untouched password in the form.
    await page
      .getByRole("textbox", { name: /Display name|display name/ })
      .first()
      .fill(`Kept ${id}`);
    await page.getByRole("button", { name: /^Save$/ }).click();
    await expect(page.getByRole("button", { name: /^Edit$/ })).toBeVisible();

    const [after] = await fetchVars(page, id);
    // Not rewritten at all: same row, same timestamp, still set.
    expect(after).toMatchObject({ name: "KEEP_ME", has_value: true });
    expect(after.updated_at).toBe(before.updated_at);
  });

  test("Clear saves the password as empty", async ({ page }) => {
    const id = uniqueId("clear");
    await seedUpstreamWithPassword(page, id, "CLEAR_ME", "clear-value-1234567890");

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByTitle(/Replace value/).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Edit CLEAR_ME/ });
    await dialog.getByLabel(/Clear the saved value/).check();
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    await expect(page.getByText("empty", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /^Save$/ }).click();

    await expect
      .poll(async () => (await fetchVars(page, id))[0]?.has_value, {
        timeout: 10_000,
      })
      .toBe(false);
  });

  test("rename moves the saved password to the new name", async ({ page }) => {
    const id = uniqueId("rename");
    const SECRET = "rename-value-1234567890";
    await seedUpstreamWithPassword(page, id, "OLD_NAME", SECRET);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByTitle(/Replace value/).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Edit OLD_NAME/ });
    const name = dialog.getByLabel(/Name/i);
    await expect(name).toHaveValue("OLD_NAME");
    await name.fill("NEW_NAME");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Optimistic display: the row moved and still reads as set.
    await expect(page.getByText("NEW_NAME")).toBeVisible();
    await expect(page.getByText("•••• set")).toBeVisible();
    await page.getByRole("button", { name: /^Save$/ }).click();

    await expect
      .poll(async () => (await fetchVars(page, id)).map((s) => s.name), {
        timeout: 10_000,
      })
      .toEqual(["NEW_NAME"]);
    const text = await fetchVarsText(page, id);
    expect(text).not.toContain(SECRET);
    const [row] = JSON.parse(text) as VarRow[];
    expect(row).toMatchObject({ is_secret: true, has_value: true });
  });
});
