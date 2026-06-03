import { test, expect, type Request } from "@playwright/test";
import { createOrg, loginAs } from "./helpers";

/**
 * Regression for the silent-abort bug surfaced by the athena-decision
 * production incident.
 *
 * Repro shape: a brand-new user lands on /signup, types only the
 * "Organization name" field, the slug auto-suggests to a value that
 * already exists in another org, and the user clicks "Create
 * organization" without ever focusing the slug input. On the buggy
 * version of the page the submit handler silently early-returned
 * (no POST, no error UI, no spinner), leaving the user staring at
 * an unchanged form. On the fixed version the server-side "taken"
 * hint is visible immediately, the Create button is disabled, and
 * the click never produces a POST.
 *
 * What this spec asserts (so it fails the moment the asymmetry comes
 * back):
 *
 * 1. The slug field is never focused — only the name field is.
 * 2. The check-slug request returns ``available: false``.
 * 3. The reason text is visible without any slug-field interaction.
 * 4. The Create button is disabled.
 * 5. Clicking the (disabled) button does NOT fire ``POST /api/orgs``.
 */

// Generate fresh slugs / emails per run so reruns don't collide with
// orgs left over from a previous test invocation. The slug stays
// within the SLUG_REGEX the SignupPage enforces locally
// (^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$).
const stamp = Date.now().toString(36);
const TAKEN_SLUG = `taken-${stamp}`;
// suggestSlug() lowercases + collapses non-alphanumerics to ``-``,
// so this display-name reproduces the taken slug exactly.
const TAKEN_DISPLAY_NAME = `Taken ${stamp}`;
const SEED_EMAIL = `seed-${stamp}@test.com`;
const NEW_USER_EMAIL = `newuser-${stamp}@test.com`;

test("signup: blocks click when auto-suggested slug is already taken", async ({
  page,
  request,
}) => {
  // ---- Seed: an org already exists at this slug -------------------
  await createOrg(request, SEED_EMAIL, TAKEN_SLUG, "Existing Seed Org");

  // ---- Login as a brand-new user (no memberships) -----------------
  await loginAs(page, NEW_USER_EMAIL);

  // Record any POST /api/orgs the page makes — the bug is precisely
  // that a stuck UI does NOT send one. If the fix breaks the other
  // way (sends POST that the server rejects), we want to see that.
  const orgsPosts: Request[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && new URL(req.url()).pathname === "/api/orgs") {
      orgsPosts.push(req);
    }
  });

  await page.goto("/");
  await expect(
    page.getByText("Create your organization"),
  ).toBeVisible({ timeout: 10_000 });

  // ---- Type only the "Organization name" field --------------------
  // We deliberately never click / focus the "Short name" input — the
  // slugTouched flag must stay false to reproduce the original bug.
  await page.getByLabel("Organization name").fill(TAKEN_DISPLAY_NAME);

  // The auto-suggest derives the slug from the name.
  await expect(page.getByLabel("Short name")).toHaveValue(TAKEN_SLUG);

  // ---- Wait for the server-side slug check ------------------------
  const checkResponse = await page.waitForResponse(
    (r) =>
      r.url().endsWith(`/api/orgs/check-slug/${TAKEN_SLUG}`) &&
      r.status() === 200,
    { timeout: 5_000 },
  );
  const body = (await checkResponse.json()) as {
    available: boolean;
    reason: string | null;
  };
  expect(body.available).toBe(false);
  expect(body.reason).not.toBeNull();

  // ---- Assertions: error must be visible AND button must be disabled
  // The user has NOT touched the slug field. On the buggy version,
  // both of these failed (slugTouched gated the FieldHint *and* the
  // disabled rule, while the submit handler aborted on slugHint
  // alone — the asymmetry that caused athena-decision).
  await expect(page.getByText(body.reason!)).toBeVisible();

  const createButton = page.getByRole("button", {
    name: /create organization/i,
  });
  await expect(createButton).toBeDisabled();

  // ---- Try to click anyway and confirm no POST is sent ------------
  // ``click({ trial: true })`` would skip a disabled element, so we
  // call ``dispatchEvent`` to force a click and prove the form's
  // own logic blocks the submit.
  await createButton.dispatchEvent("click");
  // Give any in-flight handler a tick to fire a request.
  await page.waitForTimeout(250);
  expect(orgsPosts).toHaveLength(0);

  // We're still on the signup page.
  await expect(
    page.getByText("Create your organization"),
  ).toBeVisible();
});
