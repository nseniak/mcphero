/**
 * The Admin MCP and the dashboard run the same admin actions.
 *
 * Each test drives one action through the Admin MCP (bearer token, as
 * an admin's assistant would) or the dashboard, and checks the outcome
 * where the other door, or a teammate, sees it. Every case here once
 * differed between the two doors.
 *
 * Shares ``oauth-tools`` / ``oauth-tools-pu`` and the fake OAuth
 * provider with 15-admin-oauth-takeover; ``beforeEach`` frees both
 * sign-in slots the same way that spec does (every sign-in removed,
 * stopped).
 */
import { test, expect, type APIRequestContext } from "@playwright/test";

import {
  apiLoginAs,
  createOrg,
  joinAs,
  OAUTH_TEST_MCP_URL,
  resetOAuthUpstream,
  TEST_MCP_URL,
  BACKEND_URL as BACKEND,
} from "./helpers";
import {
  ADMIN,
  ADMIN_2,
  ORG,
  callAdminTool,
  dashboardSignIn,
  slotOwner,
} from "./_admin_mcp_helpers";

const SUPERADMIN = "superadmin@example.com";

/** Org slugs ``email`` sees in their org switcher. */
async function orgSlugs(
  api: APIRequestContext,
  email: string,
): Promise<string[]> {
  await apiLoginAs(api, email);
  const resp = await api.get(`${BACKEND}/api/orgs`);
  expect(resp.status()).toBe(200);
  return (await resp.json()).orgs.map((o: { slug: string }) => o.slug);
}

/** ``email``'s row in the superadmin user list (built from membership
 *  rows), or ``undefined`` when they belong to no org. */
async function membershipRow(
  api: APIRequestContext,
  email: string,
): Promise<{ org_count: number; roles: string[] } | undefined> {
  await apiLoginAs(api, SUPERADMIN);
  const resp = await api.get(`${BACKEND}/api/superadmin/users`);
  expect(resp.status()).toBe(200);
  return (await resp.json()).users.find(
    (u: { email: string }) => u.email === email,
  );
}

async function removeQuietly(api: APIRequestContext, email: string) {
  await apiLoginAs(api, ADMIN);
  await api
    .delete(`${BACKEND}/api/admin/users/${encodeURIComponent(email)}`)
    .catch(() => {});
}

test.beforeEach(async ({ request }) => {
  await request.post(`${OAUTH_TEST_MCP_URL}/test/reset`);
  await resetOAuthUpstream(request, "oauth-tools");
  await resetOAuthUpstream(request, "oauth-tools-pu");
});

test("a teammate removed through the Admin MCP leaves the org", async ({
  request,
}) => {
  const email = "parity-removed@example.com";
  await removeQuietly(request, email);
  try {
    const added = JSON.parse(
      await callAdminTool(request, ADMIN, "add_user", { email, role: "user" }),
    );
    expect(added.status).toBe("pending");
    // Accepting the invitation turns it into a membership.
    await joinAs(request, email, ORG);
    expect(await orgSlugs(request, email)).toContain(ORG);

    const text = await callAdminTool(request, ADMIN, "remove_user", { email });

    expect(text).toBe(`User '${email}' removed.`);
    expect(await membershipRow(request, email)).toBeUndefined();
  } finally {
    await removeQuietly(request, email);
  }
});

test("a role change through the Admin MCP reaches the membership", async ({
  request,
}) => {
  const email = "parity-promoted@example.com";
  await removeQuietly(request, email);
  try {
    await callAdminTool(request, ADMIN, "add_user", { email, role: "user" });
    await joinAs(request, email, ORG);

    const changed = JSON.parse(
      await callAdminTool(request, ADMIN, "set_user_role", {
        email,
        role: "admin",
      }),
    );

    expect(changed.status).toBe("active");
    expect((await membershipRow(request, email))?.roles).toEqual(["admin"]);
  } finally {
    await removeQuietly(request, email);
  }
});

test("the dashboard calls a teammate who never signed in pending", async ({
  request,
}) => {
  const email = "parity-pending@example.com";
  await removeQuietly(request, email);
  try {
    const added = await request.post(`${BACKEND}/api/admin/users`, {
      data: { email, role: "user" },
    });
    expect(added.status()).toBe(201);
    expect((await added.json()).status).toBe("pending");

    const changed = await request.put(
      `${BACKEND}/api/admin/users/${encodeURIComponent(email)}/role`,
      { data: { role: "admin" } },
    );
    expect(changed.status()).toBe(200);
    expect((await changed.json()).status).toBe("pending");
  } finally {
    await removeQuietly(request, email);
  }
});

test("a server added through the Admin MCP stays stopped until start_upstream", async ({
  request,
}) => {
  const id = "parity-http";
  await callAdminTool(request, ADMIN, "remove_upstream", { mcp_id: id });
  try {
    const added = await callAdminTool(request, ADMIN, "add_upstream", {
      mcp_id: id,
      display_name: "Parity HTTP",
      transport: "streamable_http",
      url: `${TEST_MCP_URL}/mcp`,
    });
    expect(added).toContain("added, stopped");
    const status = JSON.parse(
      await callAdminTool(request, ADMIN, "upstream_status", {}),
    );
    expect(status[id]).toBe("stopped");

    // A stopped server is not started by anything but a Start: Refresh
    // tools refuses it, like the dashboard's button.
    const refreshed = await callAdminTool(
      request, ADMIN, "refresh_upstream_tools", { mcp_id: id },
    );
    expect(refreshed).toContain("is not running");

    const started = await callAdminTool(request, ADMIN, "start_upstream", {
      mcp_id: id,
    });
    expect(started).toMatch(/started\. [1-9]\d* tools available/);
    await apiLoginAs(request, ADMIN);
    const detail = await request.get(`${BACKEND}/api/admin/upstreams/${id}`);
    expect((await detail.json()).ready).toBe(true);

    // Starting it again leaves the running server alone.
    const again = await callAdminTool(request, ADMIN, "start_upstream", {
      mcp_id: id,
    });
    expect(again).toMatch(/is already running\. [1-9]\d* tools available/);
  } finally {
    await callAdminTool(request, ADMIN, "remove_upstream", { mcp_id: id });
  }
});

test("Admin MCP connect is refused while another admin holds the per-user slot", async ({
  request,
}) => {
  await dashboardSignIn(request, "oauth-tools-pu", ADMIN_2);

  const text = await callAdminTool(request, ADMIN, "connect_upstream", {
    mcp_id: "oauth-tools-pu",
  });

  expect(text).toContain(ADMIN_2);
  expect(text).toContain("already signed in");
  await apiLoginAs(request, ADMIN);
  expect(await slotOwner(request, "oauth-tools-pu")).toBe(ADMIN_2);
});

test("Admin MCP disconnect stops the server and keeps the admin sign-in", async ({
  request,
}) => {
  await dashboardSignIn(request, "oauth-tools", ADMIN_2);

  const text = await callAdminTool(request, ADMIN, "disconnect_upstream", {
    mcp_id: "oauth-tools",
  });

  expect(text).toBe("Upstream MCP 'oauth-tools' disconnected.");
  await apiLoginAs(request, ADMIN);
  const detail = await (
    await request.get(`${BACKEND}/api/admin/upstreams/oauth-tools`)
  ).json();
  expect(detail.stopped).toBe(true);
  expect(detail.ready).toBe(false);
  // Kept, as on the dashboard: Start brings it back with no sign-in.
  expect(detail.slot_owner).toBe(ADMIN_2);
});

test("import refuses a sandbox size the plan does not allow", async ({
  request,
}) => {
  // Import copies a server's CPU / RAM, so it must apply the same size
  // check as a single add. A fresh org (new orgs start on Free, which
  // allows only 1 vCPU / 1024 MB) keeps other specs' servers from
  // tripping the stdio count limit first.
  const stamp = Date.now().toString(36);
  const owner = `parity-free-${stamp}@example.com`;
  const slug = `parity-free-${stamp}`;
  await createOrg(request, owner, slug, "Parity Free");
  expect(
    (await request.post(`${BACKEND}/api/orgs/${slug}/switch`)).status(),
  ).toBe(204);
  try {
    const resp = await request.post(
      `${BACKEND}/api/admin/upstreams/import/confirm`,
      {
        data: {
          data: {
            mcpServers: {
              big: { command: "echo", cpu_vcpus: 4, memory_mb: 8192 },
            },
          },
          entries: [
            {
              scope: "standard",
              project_path: null,
              original_id: "big",
              target_id: "big",
            },
          ],
        },
      },
    );

    expect(resp.status()).toBe(402);
    expect((await resp.json()).gate).toBe("allowed_sandbox_combos");
    const listing = await request.get(`${BACKEND}/api/admin/upstreams`);
    expect((await listing.json()).map((u: { id: string }) => u.id))
      .not.toContain("big");
  } finally {
    await request.delete(`${BACKEND}/api/orgs/${slug}`);
  }
});
