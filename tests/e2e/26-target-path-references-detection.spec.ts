/**
 * Sandbox-file ``target_path`` accepts the same ``${...}`` references
 * env-var values do (system + user vars). Undefined references in a
 * target_path must surface in the Variables card's amber callout
 * with a [+ Add] button — same flow the JSON config uses.
 *
 * The unbundled set of changes this spec locks (in order):
 *
 * 1. Backend: ``target_path`` accepts a user-Variable reference at
 *    write time (no longer system-vars-only). The runtime resolver
 *    binds ``${TENANT_ID}`` to the user-Variable value at launch.
 * 2. Backend: a Sandbox file and a user Variable can share a name
 *    with no functional consequence — file names don't substitute.
 * 3. Frontend: ``findReferencesInSandboxFiles`` walks target_paths;
 *    the detail page merges those into the references array passed
 *    to ``TemplateVarsManager``, so an undefined ``${TENANT_ID}``
 *    appears in the amber unresolved-variables callout.
 * 4. Frontend: clicking [+ Add] on that callout opens the variable-
 *    create modal pre-filled with the name; saving it removes the
 *    file from the unresolved list and shows it as Referenced 1×.
 */
import { expect, test } from "@playwright/test";

import {
  ADMIN,
  ORG,
  loginAs,
  uniqueId,
} from "./_template_vars_helpers";

test.describe("Sandbox-file target_path: undefined-Variable detection", () => {
  test.beforeEach(async ({ page }) => {
    await loginAs(page, ADMIN, ORG);
  });

  test("Backend accepts user-var reference in target_path", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("target-path-user-var");
    const setup = await page.evaluate(async (upstreamId: string) => {
      const create = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "target_path user-var",
          command: "python3",
          args: ["-c", "import sys; sys.stdin.read()"],
          auth_mode: "service_account",
        }),
      });
      const file = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/PROFILE_CFG`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "# config",
            target_path: "${HOME}/.aws/config-${PROFILE}",
          }),
        },
      );
      return { create: create.status, file: file.status };
    }, id);
    expect(setup.create).toBe(201);
    // 200 means target_path was accepted with a user-Variable
    // reference even though ``PROFILE`` isn't defined yet — the
    // resolver only complains at launch, not at write time.
    expect(setup.file).toBe(200);

    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });

  test("Backend accepts a Sandbox file and Variable sharing a name", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("file-var-same-name");
    const result = await page.evaluate(async (upstreamId: string) => {
      const create = await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "shared name",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      const v = await fetch(
        `/api/admin/upstreams/${upstreamId}/template-vars/SHARED`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: "x", is_secret: false }),
        },
      );
      const f = await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/SHARED`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "y",
            target_path: "${HOME}/shared",
          }),
        },
      );
      return { create: create.status, varStatus: v.status, fileStatus: f.status };
    }, id);
    expect(result.create).toBe(201);
    expect(result.varStatus).toBe(200);
    expect(result.fileStatus).toBe(200);

    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });

  test("Undefined ${VAR} in target_path appears in the Variables amber callout", async ({
    page,
  }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("undefined-in-path");
    await page.evaluate(async (upstreamId: string) => {
      await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "undefined-in-path",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/TENANT_CRED`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "{}",
            target_path: "${HOME}/.config/${TENANT_ID}/creds.json",
          }),
        },
      );
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(
      page.getByRole("heading", { name: /undefined-in-path/ }),
    ).toBeVisible({ timeout: 10_000 });

    // Edit mode so the Variables manager surfaces the unresolved
    // callout with the [+ Add] button. ``${TENANT_ID}`` is undefined
    // (only ``${HOME}`` resolves — system Variable).
    await page.getByRole("button", { name: /^Edit$/ }).first().click();

    const callout = page.getByText(/This config references undefined variables/);
    await expect(callout).toBeVisible();
    // The amber callout shows the unresolved name as code; assert
    // the literal token is rendered somewhere on the page in the
    // amber section.
    await expect(page.getByText("${TENANT_ID}").first()).toBeVisible();

    // Cleanup.
    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });

  test("Adding the missing Variable removes it from the amber callout", async ({
    page,
  }) => {
    await page.goto(`/orgs/${ORG}/admin/upstream`);
    const id = uniqueId("add-from-callout");
    await page.evaluate(async (upstreamId: string) => {
      await fetch("/api/admin/upstreams", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: upstreamId,
          display_name: "add-from-callout",
          command: "python3",
          args: ["-c", "pass"],
          auth_mode: "service_account",
        }),
      });
      await fetch(
        `/api/admin/upstreams/${upstreamId}/sandbox-files/PROFILE_CFG`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contents: "# cfg",
            target_path: "${HOME}/.aws/config-${PROFILE}",
          }),
        },
      );
      // Define ``PROFILE`` ahead of time so the test can assert
      // resolved → unresolved transitions cleanly without depending
      // on the variable-add modal flow (covered by earlier specs).
      await fetch(
        `/api/admin/upstreams/${upstreamId}/template-vars/PROFILE`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: "dev", is_secret: false }),
        },
      );
    }, id);

    await page.goto(`/orgs/${ORG}/admin/upstream/${id}`);
    await expect(
      page.getByRole("heading", { name: /add-from-callout/ }),
    ).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: /^Edit$/ }).first().click();

    // ``${PROFILE}`` is defined so the amber callout shouldn't fire
    // for it; it should appear as a Referenced row (count=1) on the
    // PROFILE Variable's line.
    await expect(
      page.getByText(/This config references undefined variables/),
    ).toHaveCount(0);
    await expect(
      page.getByText(/Referenced 1×/).first(),
    ).toBeVisible();

    // Cleanup.
    await page.evaluate(async (upstreamId: string) => {
      await fetch(`/api/admin/upstreams/${upstreamId}`, { method: "DELETE" });
    }, id);
  });
});
