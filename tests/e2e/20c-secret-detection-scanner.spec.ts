/**
 * Slice of the historic 20-template-vars.spec.ts trilogy:
 * "Secret detection scanner".
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

test.describe("Secret detection scanner", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Pasting a ghp_… token in env triggers the detection dialog", async ({
    page,
  }) => {
    const id = uniqueId("detect");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        {
          [id]: {
            command: "npx",
            args: ["-y", "anything"],
            env: { GITHUB_TOKEN: "ghp_examplexxxxxxxxxxxxxxxxxxxxxxxxx" },
          },
        },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Possible passwords in JSON/ });
    await expect(dialog).toBeVisible();
    // The pattern label and field path are visible.
    await expect(dialog.getByText(/github_token/)).toBeVisible();
    await expect(dialog.getByText(/env\.GITHUB_TOKEN/)).toBeVisible();
    // Move all to variables — the JSON should be rewritten and the
    // create call goes through.
    await dialog.getByRole("button", { name: /Move all to variables/ }).click();
  });

  test("Don't-show-again is per-MCP and persists across reloads", async ({
    page,
  }) => {
    const id = uniqueId("dismiss");
    await openAddForm(page);
    await fillJsonAndAdvance(
      page,
      id,
      JSON.stringify(
        {
          [id]: {
            command: "npx",
            args: ["-y", "anything"],
            env: { GITHUB_TOKEN: "ghp_examplexxxxxxxxxxxxxxxxxxxxxxxxx" },
          },
        },
        null,
        2,
      ),
    );
    await page.getByRole("button", { name: /^Add$/ }).click();
    const dialog = page
      .getByRole("dialog")
      .filter({ hasText: /Possible passwords in JSON/ });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel(/Don't show this again/).check();
    await dialog.getByRole("button", { name: /Keep in JSON anyway/ }).click();
    // The localStorage key is set per upstream id.
    const dismissed = await page.evaluate(
      (key) => localStorage.getItem(key),
      `mcpolis:secret-scan-dismissed:${id}`,
    );
    expect(dismissed).toBe("true");
  });
});

test.describe("Dirty-config banner", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("API exposes is_dirty + config_hash on the detail response", async ({
    page,
  }) => {
    const id = uniqueId("dirty-api");
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
    // Wait for the POST to actually persist the upstream before we
    // fetch its detail — clicking Add fires the POST async, so a bare
    // ``click()`` followed by ``fetch()`` races and intermittently 404s.
    await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().endsWith("/api/admin/upstreams") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: /^Add$/ }).click(),
    ]);
    // Detail response carries the new fields. ``is_dirty`` is false
    // because the upstream isn't ready (we never started it), and the
    // config_hash is a SHA-256 hex digest.
    const detail = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}`);
      return r.json();
    }, id);
    expect(detail.is_dirty).toBe(false);
    expect(typeof detail.config_hash).toBe("string");
    expect(detail.config_hash).toMatch(/^[0-9a-f]{64}$/);
  });

  test("config_hash changes when an env var is added", async ({ page }) => {
    const id = uniqueId("dirty-hash");
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
    await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().endsWith("/api/admin/upstreams") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: /^Add$/ }).click(),
    ]);
    const before = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}`);
      return r.json();
    }, id);
    // Add an env var via the API. The runtime hash includes each
    // env var's ``updated_at``, so this should bump it.
    await page.evaluate(async (upstreamId: string) => {
      await fetch(
        `/api/admin/upstreams/${upstreamId}/template-vars/SOME_VAR`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: "v1", is_secret: false }),
        },
      );
    }, id);
    const after = await page.evaluate(async (upstreamId: string) => {
      const r = await fetch(`/api/admin/upstreams/${upstreamId}`);
      return r.json();
    }, id);
    expect(after.config_hash).not.toBe(before.config_hash);
  });
});
