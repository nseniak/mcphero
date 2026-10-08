import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PendingInvitations } from "./PendingInvitations";
import type { InvitationInfo } from "../api/types";

vi.mock("../api/orgs", () => ({
  acceptInvitation: vi.fn(),
  declineInvitation: vi.fn(),
  switchOrg: vi.fn(),
}));

import { acceptInvitation, declineInvitation, switchOrg } from "../api/orgs";

function makeInvitation(): InvitationInfo {
  return { slug: "acme-corp", display_name: "Acme Corp", role: "user" };
}

function stubPageNavigation(): { reload: ReturnType<typeof vi.fn> } {
  const reload = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { href: "http://localhost/signup", reload },
  });
  return { reload };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PendingInvitations", () => {
  it("renders nothing without invitations", () => {
    const { container } = render(<PendingInvitations invitations={[]} />);
    expect(container.innerHTML).toBe("");
  });

  it("names the organization and the role the person was invited with", () => {
    render(<PendingInvitations invitations={[makeInvitation()]} />);
    expect(
      screen.getByText("Acme Corp invited you to join as user."),
    ).toBeTruthy();
  });

  it("Join accepts the invitation, then opens the organization", async () => {
    stubPageNavigation();
    vi.mocked(acceptInvitation).mockResolvedValue(undefined);
    vi.mocked(switchOrg).mockResolvedValue(undefined);
    render(<PendingInvitations invitations={[makeInvitation()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Join" }));

    await waitFor(() => expect(window.location.href).toBe("/app"));
    expect(acceptInvitation).toHaveBeenCalledWith("acme-corp");
    expect(switchOrg).toHaveBeenCalledWith("acme-corp");
  });

  it("Decline deletes the invitation and never joins", async () => {
    const { reload } = stubPageNavigation();
    vi.mocked(declineInvitation).mockResolvedValue(undefined);
    render(<PendingInvitations invitations={[makeInvitation()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Decline" }));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(declineInvitation).toHaveBeenCalledWith("acme-corp");
    expect(acceptInvitation).not.toHaveBeenCalled();
  });

  it("says so when the invitation can no longer be accepted", async () => {
    stubPageNavigation();
    vi.mocked(acceptInvitation).mockRejectedValue(new Error("404"));
    render(<PendingInvitations invitations={[makeInvitation()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Join" }));

    expect(
      await screen.findByText(/The invitation may have been withdrawn/),
    ).toBeTruthy();
    expect(switchOrg).not.toHaveBeenCalled();
  });
});
