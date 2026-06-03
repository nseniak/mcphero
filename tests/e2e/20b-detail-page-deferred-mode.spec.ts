/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Per-MCP secrets — detail page (deferred mode)".
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
  openAddForm,
  fillJsonAndAdvance,
} from "./_template_vars_helpers";

test.describe("Per-MCP secrets — detail page (deferred mode)", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Add in edit mode commits via SETTINGS Save (single PUT)", async ({
    page,
  }) => {
    const id = uniqueId("detail");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        { [id]: { url: `${TEST_MCP_URL}/mcp` } },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByRole("button", { name: /Add variable/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable|Replace / });
    await dialog.getByLabel(/Name/i).fill("BOUND_TOKEN");
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("bound-secret-value-32-chars-xyz4");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    // Optimistic display: the row is visible while still in the
    // deferred buffer (no PUT issued yet).
    await expect(page.getByText("BOUND_TOKEN")).toBeVisible();
    await expect(page.getByText(/••••xyz4/)).toBeVisible();
    // Sanity: the server doesn't have it yet.
    const beforeServer = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}/template-vars`);
      return r.json();
    }, id);
    expect(beforeServer).toEqual([]);
    // Click the SETTINGS card's Save (the env-var modal's Save is
    // gone by now, so this name is unambiguous).
    await page.getByRole("button", { name: /^Save$/ }).click();
    // After flush the server lists it.
    await expect.poll(async () => {
      const list = await page.evaluate(async (upstreamId: string) => {
        const r = await fetch(`/api/admin/upstreams/${upstreamId}/template-vars`);
        return r.json();
      }, id);
      return list.map((s: { name: string }) => s.name);
    }).toContain("BOUND_TOKEN");
  });

  test("Cancel discards every buffered Add/Replace/Delete in the session", async ({
    page,
  }) => {
    const id = uniqueId("cancel");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        { [id]: { url: `${TEST_MCP_URL}/mcp` } },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByRole("button", { name: /Add variable/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable/ });
    await dialog.getByLabel(/Name/i).fill("DRAFT_VAR");
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("draft-value-1234567890");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    await expect(page.getByText("DRAFT_VAR")).toBeVisible();
    // Click SETTINGS Cancel — the buffer is dropped and the row vanishes.
    await page.getByRole("button", { name: /^Cancel$/ }).click();
    await expect(page.getByText("DRAFT_VAR")).toHaveCount(0);
    // Server has no record either.
    const list = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}/template-vars`);
      return r.json();
    }, id);
    expect(list).toEqual([]);
  });

  test("Add+Replace+Delete inside one Edit session flush atomically on Save", async ({
    page,
  }) => {
    const id = uniqueId("rotate");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        { [id]: { url: `${TEST_MCP_URL}/mcp` } },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    // Pre-seed two server-side env vars via API so we can exercise
    // the "delete an existing row" path inside the deferred buffer.
    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}/template-vars/SEED_KEEP`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: "seed-keep-1234567890", is_secret: false }),
      });
      await fetch(`/api/admin/upstreams/${upstreamId}/template-vars/SEED_DROP`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: "seed-drop-1234567890", is_secret: false }),
      });
    }, id);
    await page.reload();
    await page.getByRole("button", { name: /^Edit$/ }).click();
    // Add a fresh row.
    await page.getByRole("button", { name: /Add variable/ }).click();
    let modal = page.getByRole("dialog").filter({ hasText: /Add variable/ });
    await modal.getByLabel(/Name/i).fill("FRESH_ADD");
    await modal
      .getByPlaceholder(/Paste value/)
      .fill("fresh-add-1234567890");
    await modal.getByRole("button", { name: /^Save$/ }).click();
    // Delete SEED_DROP via its row's Delete button. SEED_DROP is
    // unreferenced by the JSON config, so the manager skips the
    // confirm dialog and applies the buffered delete on click.
    await page
      .locator("li")
      .filter({ hasText: "SEED_DROP" })
      .getByTitle(/Delete variable/)
      .click();
    // Replace SEED_KEEP via its row's Replace button.
    await page
      .locator("li")
      .filter({ hasText: "SEED_KEEP" })
      .getByTitle(/Replace value/)
      .click();
    modal = page.getByRole("dialog").filter({ hasText: /Edit SEED_KEEP/ });
    await modal
      .getByPlaceholder(/Paste value/)
      .fill("seed-keep-rotated-1234567890");
    await modal.getByRole("button", { name: /^Save$/ }).click();
    // Click SETTINGS Save — flushes Add+Delete+Replace in one PUT.
    await page.getByRole("button", { name: /^Save$/ }).click();
    // Verify the server reflects every change.
    await expect.poll(async () => {
      const list = await page.evaluate(async (upstreamId: string) => {
        const r = await fetch(`/api/admin/upstreams/${upstreamId}/template-vars`);
        return r.json();
      }, id);
      return list.map((s: { name: string; value: string | null }) =>
        `${s.name}=${s.value ?? "•"}`,
      ).sort();
    }).toEqual([
      // Both FRESH_ADD (password) and SEED_KEEP (plain) carry their
      // plaintext in the API response now — the SPA obfuscates
      // password rows by default with the eye toggle.
      "FRESH_ADD=fresh-add-1234567890",
      "SEED_KEEP=seed-keep-rotated-1234567890",
    ]);
  });

  test("Add modal blocks duplicate names with an inline error", async ({
    page,
  }) => {
    const id = uniqueId("dup");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        { [id]: { url: `${TEST_MCP_URL}/mcp` } },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}/template-vars/EXISTING`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: "v".repeat(20), is_secret: true }),
      });
    }, id);
    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await page.getByRole("button", { name: /^Edit$/ }).click();
    await page.getByRole("button", { name: /Add variable/ }).click();
    const dialog = page.getByRole("dialog").filter({ hasText: /Add variable/ });
    await dialog.getByLabel(/Name/i).fill("EXISTING");
    await dialog
      .getByPlaceholder(/Paste value/)
      .fill("attempted-dup-1234567890");
    await dialog.getByRole("button", { name: /^Save$/ }).click();
    await expect(dialog.getByText(/already exists/i)).toBeVisible();
    await expect(dialog).toBeVisible();
  });
});
