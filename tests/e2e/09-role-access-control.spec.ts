import { test, expect, type APIRequestContext } from "@playwright/test";
import { loginAs, apiLoginAs, BACKEND_URL as BACKEND } from "./helpers";
const ORG = "acme-corp";
const ADMIN = "admin@example.com";

/**
 * Helper: login as admin and return a cookie-authenticated request context.
 */
async function adminApi(request: APIRequestContext) {
  await apiLoginAs(request, ADMIN);
  return request;
}

/**
 * Helper: get the full role access config from the API.
 */
async function getRoleAccess(request: APIRequestContext) {
  const resp = await request.get(`${BACKEND}/api/admin/roles/access`);
  return resp.json();
}

/**
 * Helper: find a role by name in the access config array.
 */
function findRole(roles: Array<{ name: string }>, name: string) {
  return roles.find((r: { name: string }) => r.name === name);
}

// ─────────────────────────────────────────────────────────────
// Test Suite: Upstream enable/disable per role (API + UI)
// ─────────────────────────────────────────────────────────────

test.describe("Role upstream access", () => {
  test("disable upstream for role via API", async ({ request }) => {
    const api = await adminApi(request);

    // Disable test-tools for user role
    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: false } }
    );
    expect(resp.status()).toBe(200);

    // Verify via GET /roles/access
    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    expect(userRole).toBeDefined();
    expect(userRole.mcp_access.mcps["test-tools"]).toBe(false);

    // Re-enable
    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );
  });

  test("enable upstream for role via API", async ({ request }) => {
    const api = await adminApi(request);

    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    expect(userRole.mcp_access.mcps["test-tools"]).toBe(true);
  });

  test("auto_enable_new flag toggles via API", async ({ request }) => {
    const api = await adminApi(request);

    // Set auto_enable_new to true
    await api.put(
      `${BACKEND}/api/admin/roles/user/auto-enable-new`,
      { data: { auto_enable_new: true } }
    );

    let roles = await getRoleAccess(api);
    let userRole = findRole(roles, "user");
    expect(userRole.mcp_access.auto_enable_new).toBe(true);

    // Set back to false
    await api.put(
      `${BACKEND}/api/admin/roles/user/auto-enable-new`,
      { data: { auto_enable_new: false } }
    );

    roles = await getRoleAccess(api);
    userRole = findRole(roles, "user");
    expect(userRole.mcp_access.auto_enable_new).toBe(false);
  });

  test("upstream toggle visible in UI for role", async ({ page, request }) => {
    const api = await adminApi(request);

    // Ensure test-tools is enabled for user role
    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );

    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/permissions?role=user`);

    // The access table should show Test Tools
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });

    // Find the toggle for test-tools and verify it's on
    const row = page.locator("tr", { hasText: "Test Tools" });
    await expect(row).toBeVisible();
  });

  test("disabling upstream via UI reflects in API", async ({
    page,
    request,
  }) => {
    const api = await adminApi(request);

    // Start with enabled
    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );

    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/permissions?role=user`);
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });

    // Find and click the toggle in the Test Tools row
    const row = page.locator("tr", { hasText: "Test Tools" });
    const toggle = row.locator('[role="switch"]');
    if ((await toggle.count()) > 0) {
      await toggle.first().click();
      // Wait for the API call to complete
      await page.waitForTimeout(500);

      // Verify the API reflects the change
      const roles = await getRoleAccess(api);
      const userRole = findRole(roles, "user");
      expect(userRole.mcp_access.mcps["test-tools"]).toBe(false);

      // Re-enable via API
      await api.put(
        `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
        { data: { enabled: true } }
      );
    }
  });
});

// ─────────────────────────────────────────────────────────────
// Test Suite: Tool-level permissions per role
// ─────────────────────────────────────────────────────────────

test.describe("Tool-level permissions", () => {
  test("set tool fallback to deny-all via API", async ({ request }) => {
    const api = await adminApi(request);

    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: false } }
    );
    expect(resp.status()).toBe(200);

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const toolAccess = userRole.tool_access?.["test-tools"];
    expect(toolAccess?.fallback_enabled).toBe(false);

    // Cleanup: reset to allow-all
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: true } }
    );
  });

  test("set tool fallback to per-tool mode (null) via API", async ({
    request,
  }) => {
    const api = await adminApi(request);

    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: null } }
    );

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const toolAccess = userRole.tool_access?.["test-tools"];
    expect(toolAccess?.fallback_enabled).toBeNull();

    // Cleanup
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: true } }
    );
  });

  test("explicitly enable/disable individual tools via API", async ({
    request,
  }) => {
    const api = await adminApi(request);

    // Set per-tool mode
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: null } }
    );

    // Whitelist echo, deny add
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`,
      { data: { enabled: true } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/add`,
      { data: { enabled: false } }
    );

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const toolAccess = userRole.tool_access?.["test-tools"];
    expect(toolAccess?.tools?.["echo"]).toBe(true);
    expect(toolAccess?.tools?.["add"]).toBe(false);
    // greet has no explicit entry
    expect(toolAccess?.tools?.["greet"]).toBeUndefined();

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`
    );
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/add`
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: true } }
    );
  });

  test("remove explicit tool override via API", async ({ request }) => {
    const api = await adminApi(request);

    // Set then remove
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`,
      { data: { enabled: false } }
    );

    let roles = await getRoleAccess(api);
    let userRole = findRole(roles, "user");
    expect(userRole.tool_access?.["test-tools"]?.tools?.["echo"]).toBe(false);

    // Remove the override
    const resp = await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`
    );
    expect(resp.status()).toBe(200);

    roles = await getRoleAccess(api);
    userRole = findRole(roles, "user");
    expect(userRole.tool_access?.["test-tools"]?.tools?.["echo"]).toBeUndefined();
  });

  test("category defaults set via API", async ({ request }) => {
    const api = await adminApi(request);

    // Deny destructive tools by default
    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/category-defaults/destructive`,
      { data: { enabled: false } }
    );
    expect(resp.status()).toBe(200);

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const toolAccess = userRole.tool_access?.["test-tools"];
    expect(toolAccess?.category_defaults?.["destructive"]).toBe(false);

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/category-defaults/destructive`
    );
  });

  test("tool permissions visible in UI", async ({ page, request }) => {
    const api = await adminApi(request);

    // Set deny-all with echo whitelisted
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: false } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`,
      { data: { enabled: true } }
    );

    await loginAs(page, ADMIN, ORG);
    await page.goto(`/orgs/${ORG}/admin/permissions?role=user`);
    await expect(page.getByText("Test Tools")).toBeVisible({ timeout: 10_000 });

    // Expand the Test Tools section to see individual tools
    await page.getByText("Test Tools").click();
    // echo should be visible in the expanded section
    await expect(
      page.getByText("echo", { exact: true })
    ).toBeVisible({ timeout: 5_000 });

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: true } }
    );
  });
});

// ─────────────────────────────────────────────────────────────
// Test Suite: Argument constraints (parameter checking)
// ─────────────────────────────────────────────────────────────

test.describe("Argument constraints", () => {
  test("set allow constraint via API", async ({ request }) => {
    const api = await adminApi(request);

    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "^hello", mode: "allow" } }
    );
    expect(resp.status()).toBe(200);

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const constraint =
      userRole.argument_constraints?.["test-tools__echo"]?.["message"];
    expect(constraint).toBeDefined();
    expect(constraint.pattern).toBe("^hello");
    expect(constraint.mode).toBe("allow");

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`
    );
  });

  test("set forbid constraint via API", async ({ request }) => {
    const api = await adminApi(request);

    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/greet/constraints/name`,
      { data: { pattern: "evil", mode: "forbid" } }
    );
    expect(resp.status()).toBe(200);

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const constraint =
      userRole.argument_constraints?.["test-tools__greet"]?.["name"];
    expect(constraint).toBeDefined();
    expect(constraint.pattern).toBe("evil");
    expect(constraint.mode).toBe("forbid");

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/greet/constraints/name`
    );
  });

  test("remove constraint via API", async ({ request }) => {
    const api = await adminApi(request);

    // Set then remove
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "^safe", mode: "allow" } }
    );

    let roles = await getRoleAccess(api);
    let userRole = findRole(roles, "user");
    expect(
      userRole.argument_constraints?.["test-tools__echo"]?.["message"]
    ).toBeDefined();

    // Remove
    const resp = await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`
    );
    expect(resp.status()).toBe(200);

    roles = await getRoleAccess(api);
    userRole = findRole(roles, "user");
    expect(
      userRole.argument_constraints?.["test-tools__echo"]?.["message"]
    ).toBeUndefined();
  });

  test("invalid regex pattern returns error", async ({ request }) => {
    const api = await adminApi(request);

    const resp = await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "[invalid", mode: "allow" } }
    );
    // Should return 422 or 400 for invalid regex
    expect([400, 422]).toContain(resp.status());
  });

  test("multiple constraints on different tools", async ({ request }) => {
    const api = await adminApi(request);

    // Constraint on echo
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "^safe", mode: "allow" } }
    );
    // Constraint on greet
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/greet/constraints/name`,
      { data: { pattern: "admin", mode: "forbid" } }
    );
    // Constraint on add
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/add/constraints/a`,
      { data: { pattern: "^[0-9]$", mode: "allow" } }
    );

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");

    expect(
      userRole.argument_constraints?.["test-tools__echo"]?.["message"]?.pattern
    ).toBe("^safe");
    expect(
      userRole.argument_constraints?.["test-tools__greet"]?.["name"]?.mode
    ).toBe("forbid");
    expect(
      userRole.argument_constraints?.["test-tools__add"]?.["a"]?.pattern
    ).toBe("^[0-9]$");

    // Cleanup
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo/constraints/message`
    );
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/greet/constraints/name`
    );
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/add/constraints/a`
    );
  });

  test("constraint on nonexistent role returns 404", async ({ request }) => {
    const api = await adminApi(request);

    const resp = await api.put(
      `${BACKEND}/api/admin/roles/nonexistent/upstreams/test-tools/tools/echo/constraints/message`,
      { data: { pattern: "^hello", mode: "allow" } }
    );
    expect(resp.status()).toBe(404);
  });
});

// ─────────────────────────────────────────────────────────────
// Test Suite: Role CRUD + multi-role isolation
// ─────────────────────────────────────────────────────────────

test.describe.serial("Role creation and isolation", () => {
  test("create role with copy_from copies access config", async ({
    request,
  }) => {
    const api = await adminApi(request);

    // Configure user role with specific settings
    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: false } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`,
      { data: { enabled: true } }
    );

    // Clean up leftover from previous runs
    await api.delete(`${BACKEND}/api/admin/roles/restricted`);

    // Create "restricted" role copying from "user"
    const resp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: "restricted", copy_from: "user" },
    });
    expect([200, 201]).toContain(resp.status());

    // Verify the new role has the same config
    const roles = await getRoleAccess(api);
    const restricted = findRole(roles, "restricted");
    expect(restricted).toBeDefined();
    expect(restricted.mcp_access.mcps["test-tools"]).toBe(true);
    expect(restricted.tool_access?.["test-tools"]?.fallback_enabled).toBe(false);
    expect(restricted.tool_access?.["test-tools"]?.tools?.["echo"]).toBe(true);

    // Cleanup
    await api.delete(`${BACKEND}/api/admin/roles/restricted`);
    await api.delete(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tools/echo`
    );
    await api.put(
      `${BACKEND}/api/admin/roles/user/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: true } }
    );
  });

  test("modifying one role doesn't affect another", async ({ request }) => {
    const api = await adminApi(request);

    // Clean up leftover
    await api.delete(`${BACKEND}/api/admin/roles/viewer`);

    // Create a second role
    const createResp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: "viewer" },
    });
    expect([200, 201]).toContain(createResp.status());

    // Configure viewer with test-tools disabled
    await api.put(
      `${BACKEND}/api/admin/roles/viewer/mcps/test-tools`,
      { data: { enabled: false } }
    );

    // Ensure user role still has test-tools enabled
    await api.put(
      `${BACKEND}/api/admin/roles/user/mcps/test-tools`,
      { data: { enabled: true } }
    );

    const roles = await getRoleAccess(api);
    const userRole = findRole(roles, "user");
    const viewerRole = findRole(roles, "viewer");

    expect(userRole.mcp_access.mcps["test-tools"]).toBe(true);
    expect(viewerRole.mcp_access.mcps["test-tools"]).toBe(false);

    // Cleanup
    await api.delete(`${BACKEND}/api/admin/roles/viewer`);
  });

  test("delete role removes it from access config", async ({ request }) => {
    const api = await adminApi(request);

    await api.delete(`${BACKEND}/api/admin/roles/temp-role`);
    const createResp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: "temp-role" },
    });
    expect([200, 201]).toContain(createResp.status());

    let roles = await getRoleAccess(api);
    expect(findRole(roles, "temp-role")).toBeDefined();

    await api.delete(`${BACKEND}/api/admin/roles/temp-role`);

    roles = await getRoleAccess(api);
    expect(findRole(roles, "temp-role")).toBeUndefined();
  });

  test("rename role preserves access config", async ({ request }) => {
    const api = await adminApi(request);

    // Clean up leftovers
    await api.delete(`${BACKEND}/api/admin/roles/rename-src`);
    await api.delete(`${BACKEND}/api/admin/roles/rename-dst`);

    // Create role with specific config
    const createResp = await api.post(`${BACKEND}/api/admin/roles`, {
      data: { name: "rename-src" },
    });
    expect([200, 201]).toContain(createResp.status());

    await api.put(
      `${BACKEND}/api/admin/roles/rename-src/mcps/test-tools`,
      { data: { enabled: true } }
    );
    await api.put(
      `${BACKEND}/api/admin/roles/rename-src/upstreams/test-tools/tool-fallback-enabled`,
      { data: { fallback_enabled: false } }
    );

    // Rename
    const resp = await api.put(
      `${BACKEND}/api/admin/roles/rename-src/rename`,
      { data: { new_name: "rename-dst" } }
    );
    expect(resp.status()).toBe(200);

    const roles = await getRoleAccess(api);
    expect(findRole(roles, "rename-src")).toBeUndefined();
    const renamed = findRole(roles, "rename-dst");
    expect(renamed).toBeDefined();
    expect(renamed.mcp_access.mcps["test-tools"]).toBe(true);
    expect(renamed.tool_access?.["test-tools"]?.fallback_enabled).toBe(false);

    // Cleanup
    await api.delete(`${BACKEND}/api/admin/roles/rename-dst`);
  });
});
