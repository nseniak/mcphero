import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OutcomeBadge } from "./AuditPage";

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
