/**
 * Account actions show on the org's own Audit page.
 *
 * - An org admin removing a teammate writes a "Removed <email>" row,
 *   from the dashboard and from the Admin MCP alike, naming the admin.
 * - An MCP Hero operator's plan change, sign-out-everywhere,
 *   clear-sign-in, and a teammate removal made through the dashboard's
 *   org switch each write a row tagged "MCP Hero operator", as
 *   docs/operator-access.md promises.
 * - The operator's cross-org audit search filters by org inside the
 *   query, so asking for 1 row of this org returns exactly 1.
 *
 * Uses the seeded ``acme-corp`` org with unique teammate emails per run,
 * and restores the org's plan at the end so later specs on the same
 * shard see the plan they expect.
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import {
  acceptInvitation,
  apiLoginAs,
  loginAs,
  makeMcpClient,
  mintMcpToken,
  startOAuthUpstreamSignedOut,
  BACKEND_URL as BACKEND,
} from "./helpers";
import { completeUserOauth, UPSTREAM as OAUTH_UPSTREAM } from "./_token_refresh_helpers";

const ORG = "acme-corp";
const ADMIN = "admin@example.com";
const SUPERADMIN = "superadmin@example.com";

type OrgRow = { id: string; slug: string; plan: string };

async function findOrg(request: APIRequestContext): Promise<OrgRow> {
  await apiLoginAs(request, SUPERADMIN);
  const resp = await request.get(`${BACKEND}/api/superadmin/orgs`);
  expect(resp.status()).toBe(200);
  const body = (await resp.json()) as { orgs: OrgRow[] };
  const row = body.orgs.find((o) => o.slug === ORG);
  if (!row) throw new Error(`org ${ORG} not in superadmin list`);
  return row;
}

async function setPlan(
  request: APIRequestContext, orgId: string, plan: string,
): Promise<void> {
  await apiLoginAs(request, SUPERADMIN);
  const resp = await request.patch(
    `${BACKEND}/api/superadmin/orgs/${orgId}/subscription`,
    { data: { plan } },
  );
  expect(resp.status()).toBe(200);
}

async function addTeammate(
  request: APIRequestContext, email: string,
): Promise<void> {
  await apiLoginAs(request, ADMIN);
  const resp = await request.post(`${BACKEND}/api/admin/users`, {
    data: { email, role: "user" },
  });
  expect([200, 201]).toContain(resp.status());
}

async function removeTeammate(
  request: APIRequestContext, email: string,
): Promise<void> {
  await apiLoginAs(request, ADMIN);
  const resp = await request.delete(
    `${BACKEND}/api/admin/users/${encodeURIComponent(email)}`,
  );
  expect(resp.status()).toBe(200);
}

/** The ``member_removed`` row for *target*, read as the org admin. */
async function removalRow(
  request: APIRequestContext, target: string,
): Promise<Record<string, unknown>> {
  await apiLoginAs(request, ADMIN);
  const resp = await request.get(
    `${BACKEND}/api/admin/audit?action=member_removed&limit=100`,
  );
  expect(resp.status()).toBe(200);
  const rows = (await resp.json()).entries as Array<Record<string, unknown>>;
  const row = rows.find((r) => r.target_user_id === target);
  if (!row) throw new Error(`no member_removed row for ${target}`);
  return row;
}

test("admin and operator account actions appear on the org Audit page", async ({
  page,
}) => {
  const request = page.request;
  const run = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const gone = `gone-${run}@example.com`;
  const kept = `kept-${run}@example.com`;
  const viaMcp = `mcp-${run}@example.com`;
  const byOperator = `op-${run}@example.com`;

  const org = await findOrg(request);
  const originalPlan = org.plan;
  try {
    // Through Free to Team: a real plan change whatever the org was on
    // (an unchanged plan writes no row), ending on Team so the extra
    // teammates never hit the Free seat cap.
    await setPlan(request, org.id, "free");
    await setPlan(request, org.id, "team");

    await addTeammate(request, gone);
    await removeTeammate(request, gone);

    // An admin removes a teammate through the Admin MCP, with a real
    // OAuth bearer: the row must name the admin, not "anonymous".
    await addTeammate(request, viaMcp);
    const adminToken = await mintMcpToken(request, ADMIN, ORG);
    const adminMcp = await makeMcpClient(adminToken, ORG, "admin-mcp");
    try {
      await adminMcp.callTool({ name: "remove_user", arguments: { email: viaMcp } });
    } finally {
      await adminMcp.close();
    }
    const mcpRow = await removalRow(request, viaMcp);
    expect(mcpRow.user_id).toBe(ADMIN);
    expect(mcpRow.actor_role ?? null).toBeNull();

    // An operator removes a teammate through the dashboard's org switch
    // (X-Org-Slug): the row is tagged as an operator's.
    await addTeammate(request, byOperator);
    await apiLoginAs(request, SUPERADMIN);
    const opDelete = await request.delete(
      `${BACKEND}/api/admin/users/${encodeURIComponent(byOperator)}`,
      { headers: { "X-Org-Slug": ORG } },
    );
    expect(opDelete.status()).toBe(200);
    const opRow = await removalRow(request, byOperator);
    expect(opRow.user_id).toBe(SUPERADMIN);
    expect(opRow.actor_role).toBe("operator");

    await addTeammate(request, kept);
    // An invited teammate becomes a member by accepting the invitation;
    // the operator's sign-out acts on memberships. The sign-in to the
    // test OAuth MCP gives the operator a real sign-in to clear: start
    // that MCP here rather than count on a spec that ran earlier.
    await startOAuthUpstreamSignedOut(request, OAUTH_UPSTREAM);
    await apiLoginAs(request, kept);
    await acceptInvitation(request, ORG);
    await completeUserOauth(request, kept);
    await apiLoginAs(request, SUPERADMIN);
    const revoke = await request.post(
      `${BACKEND}/api/superadmin/users/${encodeURIComponent(kept)}/sessions/revoke`,
    );
    expect(revoke.status()).toBe(200);
    const reauth = await request.post(
      `${BACKEND}/api/superadmin/users/${encodeURIComponent(kept)}/connections/${org.id}/${OAUTH_UPSTREAM}/reauth`,
    );
    expect(reauth.status()).toBe(200);
    expect((await reauth.json()).cleared).toBe(true);

    // Operator view: the org filter applies inside the query.
    const search = await request.get(
      `${BACKEND}/api/superadmin/audit?org_id=${org.id}&action=operator_sign_out_everywhere&limit=1`,
    );
    expect(search.status()).toBe(200);
    const entries = (await search.json()).entries as Array<Record<string, unknown>>;
    expect(entries.length).toBe(1);
    expect(entries[0].org_id).toBe(org.id);
    expect(entries[0].target_user_id).toBe(kept);

    // Customer view: the org admin sees every row on the Audit page.
    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/audit`);
    await page
      .getByRole("combobox")
      .filter({ hasText: "All actions" })
      .selectOption({ label: "Account changes" });

    await expect(page.getByText(`Removed ${gone}`)).toBeVisible();
    await expect(page.getByText(`Signed ${kept} out everywhere`)).toBeVisible();
    await expect(page.getByText(`Cleared ${kept}'s sign-in`)).toBeVisible();
    await expect(page.getByText("Changed plan: free → team").first()).toBeVisible();
    // The operator rows are tagged; the admins' own removals are not.
    const signOutRow = page
      .getByRole("row")
      .filter({ hasText: `Signed ${kept} out everywhere` });
    await expect(signOutRow.getByTestId("audit-operator-tag")).toBeVisible();
    const opRemovedRow = page
      .getByRole("row")
      .filter({ hasText: `Removed ${byOperator}` });
    await expect(opRemovedRow.getByTestId("audit-operator-tag")).toBeVisible();
    const removedRow = page.getByRole("row").filter({ hasText: `Removed ${gone}` });
    await expect(removedRow.getByTestId("audit-operator-tag")).toHaveCount(0);
  } finally {
    for (const email of [kept, viaMcp, byOperator]) {
      await removeTeammate(request, email).catch(() => {});
    }
    await setPlan(request, org.id, originalPlan);
  }
});
