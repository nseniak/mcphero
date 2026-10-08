import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OperatorTag } from "./AuditAccountAction";
import { DetailCell, OutcomeBadge } from "./AuditPage";

describe("OutcomeBadge — tool_call rows", () => {
  it("shows the deny reason under a 'denied' badge", () => {
    render(
      <OutcomeBadge
        entry={{
          action: "tool_call",
          policy_decision: "denied",
          error_message: "MCP 'slack' is disabled for user 'svc:bot'.",
        }}
      />,
    );
    expect(screen.getByText("denied")).toBeTruthy();
    expect(
      screen.getByText(/MCP 'slack' is disabled for user 'svc:bot'\./),
    ).toBeTruthy();
  });

  it("shows just the badge for an allowed call (no reason)", () => {
    render(
      <OutcomeBadge entry={{ action: "tool_call", policy_decision: "allowed" }} />,
    );
    expect(screen.getByText("allowed")).toBeTruthy();
    expect(screen.queryByText(/disabled|forbidden/)).toBeNull();
  });
});

describe("DetailCell — account action rows", () => {
  it("names the removed teammate", () => {
    render(
      <DetailCell
        entry={{ action: "member_removed", target_user_id: "bob@acme.com" }}
      />,
    );
    expect(screen.getByText("Removed bob@acme.com")).toBeTruthy();
  });

  it("shows an operator's plan change and tags the operator", () => {
    const entry = {
      action: "operator_plan_change",
      actor_role: "operator",
      detail: "free → team",
    };
    render(
      <>
        <DetailCell entry={entry} />
        <OperatorTag entry={entry} />
      </>,
    );
    expect(screen.getByText("Changed plan: free → team")).toBeTruthy();
    expect(screen.getByText("MCP Hero operator")).toBeTruthy();
  });

  it("names the member an org admin disconnected from the gateway", () => {
    render(
      <DetailCell
        entry={{
          action: "gateway_sign_in_revoked",
          target_user_id: "bob@acme.com",
        }}
      />,
    );
    expect(
      screen.getByText("Disconnected bob@acme.com from the gateway"),
    ).toBeTruthy();
  });

  it("tags nobody on an org admin's own action", () => {
    render(<OperatorTag entry={{ action: "member_removed" }} />);
    expect(screen.queryByTestId("audit-operator-tag")).toBeNull();
  });

  it.each([
    [
      { action: "member_invited", target_user_id: "bob@acme.com", detail: "developer" },
      "Invited bob@acme.com as developer",
    ],
    [
      { action: "member_role_changed", target_user_id: "bob@acme.com", detail: "user → admin" },
      "Changed bob@acme.com's role: user → admin",
    ],
    [{ action: "upstream_added", upstream_id: "github" }, "Added the MCP github"],
    [{ action: "upstream_removed", upstream_id: "github" }, "Removed the MCP github"],
    [{ action: "role_created", detail: "reader" }, "Created the role reader"],
    [{ action: "role_renamed", detail: "reader → auditor" }, "Renamed a role: reader → auditor"],
    [{ action: "role_deleted", detail: "auditor" }, "Deleted the role auditor"],
    [
      { action: "service_token_created", detail: "ci-bot (role reader)" },
      "Created the service token ci-bot (role reader)",
    ],
    [{ action: "service_token_revoked", detail: "ci-bot" }, "Revoked the service token ci-bot"],
  ])("says what an admin's change did: %o", (entry, text) => {
    render(<DetailCell entry={entry} />);
    expect(screen.getByText(text)).toBeTruthy();
  });
});

describe("OutcomeBadge — allowed calls that did not complete", () => {
  it("shows 'error' when an allowed call failed or was refused", () => {
    render(
      <OutcomeBadge
        entry={{
          action: "tool_call",
          policy_decision: "allowed",
          response_status: "error",
          error_message: "You are not signed in to Notion.",
        }}
      />,
    );
    expect(screen.getByText("error")).toBeTruthy();
    expect(screen.queryByText("allowed")).toBeNull();
    expect(screen.getByText("You are not signed in to Notion.")).toBeTruthy();
  });

  it("shows 'cancelled' when the client gave up", () => {
    render(
      <OutcomeBadge
        entry={{
          action: "tool_call",
          policy_decision: "allowed",
          response_status: "cancelled",
        }}
      />,
    );
    expect(screen.getByText("cancelled")).toBeTruthy();
  });
});
