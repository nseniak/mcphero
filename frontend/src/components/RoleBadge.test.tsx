import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";

import { RoleBadge } from "./RoleBadge";

function renderAt(path: string, node: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/orgs/:slug/*" element={node} />
        <Route path="*" element={node} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RoleBadge", () => {
  it("links to the current org's permissions page with the role tab", () => {
    renderAt("/orgs/acme/admin/service-tokens", <RoleBadge role="user" />);
    const link = screen.getByRole("link", { name: "user" });
    expect(link).toHaveAttribute(
      "href",
      "/orgs/acme/admin/permissions?role=user",
    );
  });

  it("labels and links the default role correctly", () => {
    renderAt("/orgs/acme/admin/team", <RoleBadge role="default" />);
    const link = screen.getByRole("link", { name: "Default" });
    expect(link).toHaveAttribute(
      "href",
      "/orgs/acme/admin/permissions?role=default",
    );
  });

  it("honors an explicit orgSlug override (multi-org lists)", () => {
    // Rendered on a page whose current org is `acme`, but the badge is
    // for a row belonging to `globex` — the link must target globex.
    renderAt(
      "/orgs/acme/orgs",
      <RoleBadge role="admin" orgSlug="globex" />,
    );
    const link = screen.getByRole("link", { name: "admin" });
    expect(link).toHaveAttribute(
      "href",
      "/orgs/globex/admin/permissions?role=admin",
    );
  });

  it("falls back to the default org outside an org route", () => {
    renderAt("/some/other/path", <RoleBadge role="user" />);
    const link = screen.getByRole("link", { name: "user" });
    expect(link).toHaveAttribute(
      "href",
      "/orgs/default/admin/permissions?role=user",
    );
  });
});
