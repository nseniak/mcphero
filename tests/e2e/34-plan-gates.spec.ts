/**
 * Plan-mechanics e2e coverage.
 *
 * One spec file per the splitting convention in CLAUDE.md; shared
 * fixtures live in ``_plan_gates_helpers.ts``. Each ``test()`` block
 * maps to one of the 14 scenarios in the verification section of
 * the plan-mechanics implementation plan.
 *
 * The harness already seeds ``acme-corp`` with three teammates
 * (admin + nseniak + admin2) and one HTTP upstream. So the org sits
 * exactly at the Free seat cap on cold-boot — perfect for the
 * worked-example scenario without any extra seeding.
 *
 * Tests that need to seed past Free's caps flip to Team first,
 * seed under Team, then flip back to Free for the assertion.
 */
import { test } from "@playwright/test";
import {
  ADMIN,
  ORG,
  SUPERADMIN,
  expect,
  flipPlan,
  loginWithPlan,
  resetSeededBaseline,
  upgradeDialogTitle,
} from "./_plan_gates_helpers";
import { BACKEND_URL, loginAs, apiLoginAs } from "./helpers";

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  await flipPlan(page.request, "free");
  // Return the org to its seeded cold-boot state. This serial group's
  // tests aren't individually idempotent (test 1 adds a member, the
  // gate tests seed upstreams); without resetting, a single flake mid-run
  // makes the Playwright retry of the whole group fail with stale-state
  // 409s instead of recovering.
  await resetSeededBaseline(page.request);
});

test.afterAll(async ({ request }) => {
  // Restore the shard's seeded Team plan so other spec files
  // (which all assume the seeded "no caps" state) keep working.
  // Without this, any spec the runner schedules after this file on
  // the same shard inherits the Free state of the last test here
  // and starts hitting unexpected 402s.
  await flipPlan(request, "team");
});

test("1 — seat gate: Free org at 3 members, 4th Add Member opens the upgrade dialog", async ({ page }) => {
  // Org already has 3 seeded teammates — exactly at the Free cap.
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  const resp = await page.request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: "fourth@example.com", role: "user" },
  });
  expect(resp.status()).toBe(402);
  const body = await resp.json();
  expect(body.gate).toBe("max_seats");
  expect(body.message).toContain("3 teammates");
  // Flip and retry — fourth add now succeeds.
  await flipPlan(page.request, "team");
  const ok = await page.request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: "fourth@example.com", role: "user" },
  });
  expect(ok.status()).toBe(201);
});

test("2 — http MCP gate: Free org adding 6th HTTP upstream returns 402", async ({ page }) => {
  await flipPlan(page.request, "team");
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  // Seed under Team so the count gate doesn't intercept seeding.
  // 1 already seeded (test-tools); add 4 more to land at Free's cap.
  for (let i = 0; i < 4; i += 1) {
    const r = await page.request.post(`${BACKEND_URL}/api/admin/upstreams`, {
      data: {
        id: `seed-http-${i}`,
        display_name: `Seed HTTP ${i}`,
        url: `http://localhost:9000/${i}/mcp`,
        auth_mode: "service_account",
      },
    });
    if (r.status() !== 201 && r.status() !== 409) {
      throw new Error(`seed http ${i}: ${r.status()} ${await r.text()}`);
    }
  }
  // Flip back to Free, attempt the 6th.
  await flipPlan(page.request, "free");
  const sixth = await page.request.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: "seed-http-blocked",
      display_name: "Seed HTTP Blocked",
      url: "http://localhost:9000/blocked/mcp",
      auth_mode: "service_account",
    },
  });
  expect(sixth.status()).toBe(402);
  expect((await sixth.json()).gate).toBe("max_http_upstreams");
});

test("3 — stdio MCP gate: Free org with 1 stdio, second add returns 402", async ({ page }) => {
  await flipPlan(page.request, "team");
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  await page.request.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: "seed-stdio-0",
      display_name: "Seed Stdio 0",
      command: "echo",
      auth_mode: "service_account",
    },
  });
  await flipPlan(page.request, "free");
  const second = await page.request.post(`${BACKEND_URL}/api/admin/upstreams`, {
    data: {
      id: "seed-stdio-1",
      display_name: "Seed Stdio 1",
      command: "echo",
      auth_mode: "service_account",
    },
  });
  expect(second.status()).toBe(402);
  expect((await second.json()).gate).toBe("max_stdio_upstreams");
});

test("4 — custom role gate: Free org cannot create a third role", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  const resp = await page.request.post(`${BACKEND_URL}/api/admin/roles`, {
    data: { name: "viewer" },
  });
  expect(resp.status()).toBe(402);
  expect((await resp.json()).gate).toBe("max_custom_roles");
});

test("5 — argument constraint gate: Free org cannot set any constraint", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  // Use the seeded ``test-tools`` upstream — it's always present.
  const resp = await page.request.put(
    `${BACKEND_URL}/api/admin/roles/admin/upstreams/test-tools/tools/anything/constraints/x`,
    { data: { pattern: ".*", mode: "allow" } },
  );
  expect(resp.status()).toBe(402);
  expect((await resp.json()).gate).toBe("allow_argument_constraints");
});

test("6 — sandbox capabilities marks off-plan combos disabled", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  const caps = await page.request.get(
    `${BACKEND_URL}/api/admin/sandbox/capabilities`,
  );
  expect(caps.status()).toBe(200);
  const data = await caps.json();
  const combos: Array<{ cpu_vcpus: number; memory_mb: number; enabled: boolean }>
    = data.allowed_combinations;
  // (1, 1024) is the only enabled pair on Free — others are visible
  // but disabled so the upgrade-modal-less "Team plan" UI surfaces
  // them correctly.
  for (const combo of combos) {
    const pair: [number, number] = [Math.trunc(combo.cpu_vcpus), combo.memory_mb];
    if (pair[0] === 1 && pair[1] === 1024) {
      expect(combo.enabled).toBe(true);
    } else {
      expect(combo.enabled).toBe(false);
    }
  }
});

test("7 — audit retention: Free vs Team caps are different at the API", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await apiLoginAs(page.request, ADMIN);
  const free = await page.request.get(`${BACKEND_URL}/api/admin/audit?limit=200`);
  expect(free.status()).toBe(200);
  await flipPlan(page.request, "team");
  const team = await page.request.get(`${BACKEND_URL}/api/admin/audit?limit=200`);
  expect(team.status()).toBe(200);
  // Both endpoints respond — the retention numerics are unit-tested
  // (Free limits to 30 days, Team to 365). The e2e covers the
  // route-plumbing, not the time math.
});

test("8 — plan badge: Free renders clickable pill, Team renders static pill", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await page.goto(`/orgs/${ORG}/admin/upstream`);
  const freePill = page.getByTestId("plan-badge");
  await expect(freePill).toBeVisible();
  await expect(freePill).toHaveAttribute("data-plan", "free");
  await freePill.click();
  await expect(upgradeDialogTitle(page)).toBeVisible();
  await page.getByRole("button", { name: "Close", exact: true }).last().click();
  await flipPlan(page.request, "team");
  await page.reload();
  const teamPill = page.getByTestId("plan-badge");
  await expect(teamPill).toHaveAttribute("data-plan", "team");
  await expect(teamPill).toContainText(/Team plan/i);
});

test("9 — Plan column on /orgs/manage shows correct label per row", async ({ page }) => {
  await loginAs(page, ADMIN, ORG);
  await page.goto(`/orgs/manage`);
  await expect(page.getByRole("heading", { name: /Organizations/i })).toBeVisible();
  // Scope to acme-corp's row, NOT ``.first()``. The dashboard chrome
  // renders its own ``plan-badge``, AND co-located specs on the same
  // shard can leave extra orgs admin@example.com belongs to — either
  // would win ``.first()`` and assert against the wrong org's plan. The
  // row is identified by its slug span (rendered as exact text).
  const rowBadge = () =>
    page
      .locator("tbody tr")
      .filter({ has: page.getByText(ORG, { exact: true }) })
      .getByTestId("plan-badge");
  await expect(rowBadge()).toHaveAttribute("data-plan", "free");
  await flipPlan(page.request, "team");
  await page.reload();
  await expect(rowBadge()).toHaveAttribute("data-plan", "team");
});

test("10 — Plan column + flip on superadmin list", async ({ page }) => {
  await loginAs(page, SUPERADMIN, ORG);
  await page.goto(`/superadmin/orgs`);
  // Scope to acme-corp's row, NOT ``.first()``. Co-located specs on the
  // same shard can leave extra orgs in the superadmin list, and the table
  // re-renders (and may re-sort) after the PATCH lands — so ``.first()``
  // can select one org and then poll a *different* row's value, which
  // stays "free" forever ("Expected team, Received free").
  const row = page.locator("tbody tr").filter({
    has: page.getByRole("link", { name: ORG, exact: true }),
  });
  const select = row.getByTestId("superadmin-plan-select");
  await expect(select).toBeVisible();
  await select.selectOption("team");
  // The select is controlled by query data, so its value reflects "team"
  // only once the PATCH + refetch round-trips — which can lag under
  // cross-suite load. 20s stays under the 30s standalone test timeout
  // while giving the refetch room when the box is busy.
  await expect.poll(async () => {
    return row.getByTestId("superadmin-plan-select").inputValue();
  }, { timeout: 20_000 }).toBe("team");
});

test("11 — public /pricing while signed out", async ({ page, context }) => {
  await context.clearCookies();
  await page.goto(`/pricing`);
  await expect(page.getByRole("heading", { name: /Simple pricing/i })).toBeVisible();
  await expect(page.getByText("Free", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Team", { exact: true }).first()).toBeVisible();
});

test("12 — /pricing while signed in on Free shows current-plan badge + Upgrade CTA", async ({ page }) => {
  await loginWithPlan(page, "free");
  await page.goto(`/pricing`);
  await expect(page.getByTestId("current-plan-badge")).toBeVisible();
  await page.getByRole("button", { name: /^Upgrade$/i }).click();
  await expect(upgradeDialogTitle(page)).toBeVisible();
});

test("13 — /pricing on Team shows Team current-plan badge", async ({ page }) => {
  await loginWithPlan(page, "team");
  await page.goto(`/pricing`);
  await expect(page.getByTestId("current-plan-badge")).toBeVisible();
});

test("14 — flipping back to Free leaves over-limit data; new add trips gate", async ({ page }) => {
  await loginWithPlan(page, "team");
  await apiLoginAs(page.request, ADMIN);
  // Org already has 3 seeded users; pile a few more under Team.
  for (let i = 0; i < 5; i += 1) {
    await page.request.post(`${BACKEND_URL}/api/admin/users`, {
      data: { email: `team-extra-${i}@example.com`, role: "user" },
    });
  }
  await flipPlan(page.request, "free");
  const resp = await page.request.post(`${BACKEND_URL}/api/admin/users`, {
    data: { email: "still-blocked@example.com", role: "user" },
  });
  expect(resp.status()).toBe(402);
});
