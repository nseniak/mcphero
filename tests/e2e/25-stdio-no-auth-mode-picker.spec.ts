/**
 * Stdio MCPs are always service_account — the auth-mode picker
 * doesn't apply. Lock three contracts:
 *   1. Create wizard step 2 hides the picker when the JSON config
 *      carries a ``command`` field.
 *   2. Detail page hides the Authentication field for stdio
 *      upstreams in both view and edit modes.
 *   3. The backend rejects ``stdio + admin_oauth`` /
 *      ``stdio + per_user_oauth`` at write time so the admin MCP
 *      tool / a direct API caller can't persist a non-functional
 *      shape either.
 */
import { expect, test } from "@playwright/test";

import {
  ADMIN,
  ORG,
  loginAs,
  openAddForm,
  uniqueId,
} from "./_template_vars_helpers";

test.describe("Stdio + auth-mode picker invariant", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Create wizard hides auth-mode picker for stdio JSON", async ({ page }) => {
    await openAddForm(page);
    await page
      .locator("textarea")
      .first()
      .fill(
        JSON.stringify(
          {
            "demo-stdio": {
              command: "npx",
              args: ["-y", "@modelcontextprotocol/server-everything"],
            },
          },
          null,
          2,
        ),
      );
    // Step 1 surfaces the stdio-auth callout next to the JSON box —
    // covered by 20a-create-wizard-buffered-mode.spec.ts. We focus
    // here on step 2 (Details), which is where the auth-mode picker
    // lives for HTTP and must NOT for stdio.
    await page.getByRole("button", { name: /^Next/ }).click();
    await expect(
      page.getByText(/^Identity$/),
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText(/User authentication mode/i),
    ).toHaveCount(0);
    await expect(
      page.getByRole("radio", { name: /Per-user|Shared|None/ }),
    ).toHaveCount(0);
  });

  test("Detail page hides Authentication field for stdio upstreams", async ({ page }) => {
    const id = uniqueId("stdio-no-picker");
    // Seed an stdio upstream via the API. ``page.request`` shares
    // the page's session cookie (set by ``loginAs``) so the call is
    // authenticated.
    const resp = await page.request.post("/api/admin/upstreams", {
      data: {
        id,
        display_name: "Stdio no picker",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-everything"],
        auth_mode: "service_account",
      },
    });
    expect(resp.status()).toBe(201);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(
      page.getByRole("heading", { name: /Stdio no picker/ }),
    ).toBeVisible({ timeout: 10_000 });
    // View mode: no Authentication label at all (stdio is implicitly
    // service_account; rendering it would just clutter the page).
    await expect(
      page.getByText(/User authentication mode/i),
    ).toHaveCount(0);

    // Edit mode: the radio picker is also suppressed.
    await page.getByRole("button", { name: /^Edit$/ }).first().click();
    await expect(
      page.getByText(/User authentication mode/i),
    ).toHaveCount(0);
    await expect(
      page.getByRole("radio", { name: /Per-user|Shared|None/ }),
    ).toHaveCount(0);

    // Cleanup.
    const del = await page.request.delete(`/api/admin/upstreams/${id}`);
    expect([200, 204]).toContain(del.status());
  });

  test("Backend rejects stdio + admin_oauth at write time", async ({ page }) => {
    const resp = await page.request.post("/api/admin/upstreams", {
      data: {
        id: uniqueId("bad-combo"),
        display_name: "Bad combo",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-everything"],
        auth_mode: "admin_oauth",
      },
    });
    expect(resp.status()).toBe(400);
    const body = await resp.json();
    const text = typeof body === "string" ? body : JSON.stringify(body);
    expect(text).toMatch(/service_account/);
  });
});
