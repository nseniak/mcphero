/**
 * Phase 0 red test for the "stop comparing role names with the
 * literal 'admin'" refactor.
 *
 * The `PlanBadge` upgrade-CTA gate at PlanBadge.tsx:52 reads
 * `role !== "admin"` to decide whether to render the upgrade
 * button vs. the static "Free plan" label. That reduction makes
 * the gate fail for users whose role is admin via the
 * `is_admin=True` flag but whose role *name* is something other
 * than the literal "admin" (e.g. "operator", "owner"). The fix
 * routes the gate through an `is_admin` boolean — either a
 * `isAdmin` prop or `user.current_org.is_admin` from `useAuth()`.
 *
 * This test fails today and turns green once the gate is
 * migrated to consume `is_admin`.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PlanBadge } from "./PlanBadge";
import { AuthContext } from "../../hooks/useAuth";
import type { UserInfo } from "../../api/types";

function makeAdminViaFlagUser(roleName: string): UserInfo {
  return {
    email: "operator@example.com",
    roles: [roleName],
    is_admin: true,
    is_superadmin: false,
    orgs: [
      {
        slug: "default",
        display_name: "Default",
        role: roleName,
        plan: "free",
        // Future field — populated server-side from the role's
        // is_admin flag. The PlanBadge gate must consult this,
        // not the role name string.
        is_admin: true,
      } as UserInfo["orgs"][number],
    ],
    current_org: {
      slug: "default",
      display_name: "Default",
      role: roleName,
      plan: "free",
      is_admin: true,
    } as UserInfo["current_org"],
  };
}

describe("PlanBadge — admin gate routes through is_admin, not role name", () => {
  it("renders the upgrade button when the user is admin via is_admin flag under a non-'admin' role name", () => {
    const user = makeAdminViaFlagUser("operator");
    render(
      <AuthContext.Provider
        value={{
          user,
          loading: false,
          error: null,
          hasUsers: true,
          logout: async () => {},
        }}
      >
        <PlanBadge />
      </AuthContext.Provider>,
    );

    // Today: PlanBadge sees role="operator" !== "admin" and falls
    // through to the static "Free plan" label (no button). After
    // the refactor: PlanBadge consults is_admin and renders the
    // upgrade button.
    expect(
      screen.getByRole("button", { name: /upgrade/i }),
    ).toBeInTheDocument();
  });

  it("does not render the upgrade button for a non-admin user even if their role is named 'admin'", () => {
    // Inverse edge: a role *named* "admin" with is_admin=false
    // must NOT grant the upgrade affordance. Pins the contract
    // that name doesn't matter — only the flag.
    const user: UserInfo = {
      email: "viewer@example.com",
      roles: ["admin"],
      is_admin: false,
      is_superadmin: false,
      orgs: [
        {
          slug: "default",
          display_name: "Default",
          role: "admin",
          plan: "free",
          is_admin: false,
        } as UserInfo["orgs"][number],
      ],
      current_org: {
        slug: "default",
        display_name: "Default",
        role: "admin",
        plan: "free",
        is_admin: false,
      } as UserInfo["current_org"],
    };
    render(
      <AuthContext.Provider
        value={{
          user,
          loading: false,
          error: null,
          hasUsers: true,
          logout: async () => {},
        }}
      >
        <PlanBadge />
      </AuthContext.Provider>,
    );

    // Today: PlanBadge sees role="admin" === "admin" and renders
    // the upgrade button — wrong, since this user isn't actually
    // admin. After the refactor: the static "Free plan" label.
    expect(screen.queryByRole("button", { name: /upgrade/i })).toBeNull();
    expect(screen.getByText(/free plan/i)).toBeInTheDocument();
  });
});
