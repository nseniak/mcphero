/**
 * An org admin's changes to the org show on its Audit page, whichever
 * door they came through.
 *
 * - Through the dashboard: a role created, renamed and deleted, and a
 *   service token created and revoked on it.
 * - Through the Admin MCP, with a real OAuth bearer: an MCP added and
 *   removed.
 *
 * Each reads as one line under "Account changes", naming what changed.
 * Uses the seeded ``acme-corp`` org (Team plan, its seeded baseline, so
 * a custom role is allowed) with names unique to the run.
 */
import { test, expect } from "@playwright/test";

import {
  apiLoginAs,
  loginAs,
  BACKEND_URL as BACKEND,
  TEST_MCP_URL,
} from "./helpers";
import { callAdminTool } from "./_admin_mcp_helpers";
import { ADMIN, ORG, flipPlan } from "./_plan_gates_helpers";

test("an admin's changes from both doors appear on the Audit page", async ({
  page,
}) => {
  const request = page.request;
  const run = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const role = `role-${run}`;
  const renamed = `renamed-${run}`;
  const label = `tok-${run}`;
  const mcp = `mcp-${run}`;

  await flipPlan(request, "team");
  await apiLoginAs(request, ADMIN);
  const created = await request.post(`${BACKEND}/api/admin/roles`, {
    data: { name: role },
  });
  expect(created.status()).toBe(201);
  const rename = await request.put(`${BACKEND}/api/admin/roles/${role}/rename`, {
    data: { new_name: renamed },
  });
  expect(rename.status()).toBe(200);
  const minted = await request.post(`${BACKEND}/api/admin/service-tokens`, {
    data: { label, role: renamed },
  });
  expect(minted.status()).toBe(201);
  const revoked = await request.delete(`${BACKEND}/api/admin/service-tokens/${label}`);
  expect(revoked.status()).toBe(200);
  const deleted = await request.delete(`${BACKEND}/api/admin/roles/${renamed}`);
  expect(deleted.status()).toBe(200);

  const added = await callAdminTool(request, ADMIN, "add_upstream", {
    mcp_id: mcp,
    display_name: "Audit MCP",
    transport: "streamable_http",
    url: `${TEST_MCP_URL}/mcp`,
  });
  expect(added).toContain("added, stopped");
  const removed = await callAdminTool(request, ADMIN, "remove_upstream", {
    mcp_id: mcp,
  });
  expect(removed).toContain("removed");

  await loginAs(page, ADMIN, ORG);
  await page.goto(`/orgs/${ORG}/admin/audit`);
  await page
    .getByRole("combobox")
    .filter({ hasText: "All actions" })
    .selectOption({ label: "Account changes" });

  for (const line of [
    `Created the role ${role}`,
    `Renamed a role: ${role} → ${renamed}`,
    `Created the service token ${label} (role ${renamed})`,
    `Revoked the service token ${label}`,
    `Deleted the role ${renamed}`,
    `Added the MCP ${mcp}`,
    `Removed the MCP ${mcp}`,
  ]) {
    await expect(page.getByText(line, { exact: true })).toBeVisible();
  }
});
