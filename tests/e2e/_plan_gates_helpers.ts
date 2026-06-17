/**
 * Shared helpers for the 34*-plan-gates split.
 *
 * The e2e harness seeds ``acme-corp`` as the default test org with
 * ``admin@example.com`` listed as both dashboard admin AND
 * superadmin (see run-e2e-tests.py). The plan-gate scenarios use the
 * same login but route through the superadmin PATCH endpoint to flip
 * plans without depending on the operator-driven dropdown.
 */
import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { BACKEND_URL, apiLoginAs, loginAs } from "./helpers";

export const ORG = "acme-corp";
export const ADMIN = "admin@example.com";
export const SUPERADMIN = "superadmin@example.com";

/** Issue the new ``PATCH /api/superadmin/orgs/{org_id}/subscription``
 *  call to flip the seeded org's plan.
 *
 *  Logs in as ``superadmin@example.com`` (the dedicated
 *  ``MCPOLIS_SUPERADMIN_EMAILS`` identity from run-e2e-tests.py).
 *  Re-runs the dashboard login as ``admin@example.com`` afterwards
 *  so the request context's session cookie returns to the test's
 *  default identity. */
export async function flipPlan(
  request: APIRequestContext,
  plan: "free" | "team",
): Promise<void> {
  await apiLoginAs(request, SUPERADMIN);
  const listResp = await request.get(`${BACKEND_URL}/api/superadmin/orgs`);
  if (listResp.status() !== 200) {
    throw new Error(
      `superadmin list failed: ${listResp.status()} ${await listResp.text()}`,
    );
  }
  const list = (await listResp.json()) as {
    orgs: Array<{ id: string; slug: string }>;
  };
  const row = list.orgs.find((o) => o.slug === ORG);
  if (!row) {
    throw new Error(`org ${ORG} not in superadmin list`);
  }
  const resp = await request.patch(
    `${BACKEND_URL}/api/superadmin/orgs/${row.id}/subscription`,
    { data: { plan } },
  );
  if (resp.status() !== 200) {
    throw new Error(
      `flipPlan failed: ${resp.status()} ${await resp.text()}`,
    );
  }
  // Restore the dashboard admin's signed cookie so subsequent
  // ``apiLoginAs(... ADMIN)`` calls (or page navigations) don't have
  // to re-authenticate.
  await apiLoginAs(request, ADMIN);
}

/** Convenience: seed the org to a specific plan, then walk the
 *  dev-stub login on the page so the SPA picks up the plan via
 *  ``/api/auth/me``. */
export async function loginWithPlan(
  page: Page,
  plan: "free" | "team",
): Promise<void> {
  await flipPlan(page.request, plan);
  await loginAs(page, ADMIN, ORG);
}

/** Artifacts the serial 34-plan-gates group adds across its tests.
 *  Cleared by ``resetSeededBaseline`` in beforeEach so every test — and
 *  every Playwright retry of the serial group — starts from the seeded
 *  cold-boot state. Without this, one flake mid-group makes the retry run
 *  against dirtied state: test 1 re-adds ``fourth@example.com`` and 409s,
 *  the upstream-gate tests 409 on their re-seeds, and the whole group
 *  fails for the wrong reason instead of recovering. */
export const ADDED_MEMBERS = ["fourth@example.com"];
export const ADDED_UPSTREAMS = [
  "seed-http-0",
  "seed-http-1",
  "seed-http-2",
  "seed-http-3",
  "seed-http-blocked",
  "seed-stdio-0",
  "seed-stdio-1",
];

/** Delete every artifact the 34-plan-gates tests add, returning the org to
 *  its seeded baseline (3 members, 3 upstreams). Deletes are idempotent —
 *  a 404 on an already-absent id is fine — and never plan-gated, so this is
 *  safe to call on either plan. */
export async function resetSeededBaseline(
  request: APIRequestContext,
): Promise<void> {
  await apiLoginAs(request, ADMIN);
  for (const id of ADDED_UPSTREAMS) {
    await request.delete(`${BACKEND_URL}/api/admin/upstreams/${id}`);
  }
  for (const email of ADDED_MEMBERS) {
    await request.delete(
      `${BACKEND_URL}/api/admin/users/${encodeURIComponent(email)}`,
    );
  }
}

/** Add ``count`` extra members to the seeded org so subsequent
 *  ``Add member`` clicks hit the seat gate. The seeded admin counts
 *  toward the cap, so passing 2 lands at exactly the Free-plan
 *  ceiling (admin + 2 = 3). */
export async function addExtraMembers(
  request: APIRequestContext,
  count: number,
): Promise<void> {
  await apiLoginAs(request, ADMIN);
  for (let i = 0; i < count; i += 1) {
    const email = `extra-${i}-${Date.now().toString(36)}-${Math.random()
      .toString(36)
      .slice(2, 6)}@example.com`;
    const resp = await request.post(`${BACKEND_URL}/api/admin/users`, {
      data: { email, role: "user" },
    });
    if (resp.status() !== 201 && resp.status() !== 200) {
      const text = await resp.text();
      if (!text.includes("already") && !text.includes("exists")) {
        throw new Error(`addExtraMembers failed: ${resp.status()} ${text}`);
      }
    }
  }
}

/** Open the upgrade dialog title locator — used across scenarios
 *  to assert the dialog actually rendered. */
export function upgradeDialogTitle(page: Page) {
  return page.getByRole("dialog").getByRole("heading", { level: 3 });
}

export { expect };
