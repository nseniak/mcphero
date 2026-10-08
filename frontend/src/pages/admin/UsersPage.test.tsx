/**
 * The Team page offers no Remove on the signed-in admin's own row,
 * whatever the letter case the admin was invited with: invited as
 * ``Admin@Acme.com``, they sign in as ``admin@acme.com``.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router";

import { UsersPage } from "./UsersPage";
import { AuthContext } from "../../hooks/useAuth";
import type { AdminUserInfo, UserInfo } from "../../api/types";

vi.mock("../../api/admin", () => ({
  fetchUsers: vi.fn(),
  fetchRoles: vi.fn(),
  addUser: vi.fn(),
  removeUser: vi.fn(),
}));
vi.mock("../../hooks/useFeatures", () => ({
  useFeatures: () => ({
    allowStdioMcp: true,
    mode: "standalone",
    sandboxProvider: "local-subprocess",
    isLoading: false,
  }),
}));

import { fetchRoles, fetchUsers } from "../../api/admin";

function makeTeammate(email: string, role = "admin"): AdminUserInfo {
  return { email, role, is_admin: role === "admin", status: "active" };
}

function makeSignedIn(email: string): UserInfo {
  return {
    email,
    roles: ["admin"],
    is_admin: true,
    is_superadmin: false,
    orgs: [],
    current_org: {
      slug: "default",
      display_name: "Default",
      role: "admin",
      is_admin: true,
      plan: "team",
    },
    invitations: [],
  };
}

function renderTeamPage(signedIn: string) {
  return render(
    <AuthContext.Provider
      value={{
        user: makeSignedIn(signedIn),
        loading: false,
        error: null,
        hasUsers: true,
        logout: async () => {},
      }}
    >
      <MemoryRouter>
        <UsersPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

async function rowOf(email: string): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name: email });
  const row = link.closest("tr");
  if (row === null) throw new Error(`no row for ${email}`);
  return row;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchUsers).mockResolvedValue([
    makeTeammate("Admin@Acme.com"),
    makeTeammate("dev@acme.com", "user"),
  ]);
  vi.mocked(fetchRoles).mockResolvedValue([
    {
      name: "admin",
      is_admin: true,
      is_default: false,
      user_count: 1,
      service_token_count: 0,
    },
    {
      name: "user",
      is_admin: false,
      is_default: true,
      user_count: 1,
      service_token_count: 0,
    },
  ]);
});

describe("UsersPage", () => {
  it("offers no Remove on the signed-in admin's own row, whatever its letter case", async () => {
    renderTeamPage("admin@acme.com");

    expect(
      within(await rowOf("Admin@Acme.com")).queryAllByRole("button"),
    ).toHaveLength(0);
    expect(
      within(await rowOf("dev@acme.com")).queryAllByRole("button"),
    ).toHaveLength(1);
  });
});
