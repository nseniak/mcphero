import { describe, expect, it } from "vitest";

import type { UpstreamSummary } from "../api/types";
import { upstreamStatusLabel } from "./upstreamStatusLabel";

function makeSummary(overrides: Partial<UpstreamSummary>): UpstreamSummary {
  return {
    id: "notion",
    display_name: "Notion",
    transport: "streamable_http",
    auth_mode: "per_user_oauth",
    ready: false,
    slot_owner: null,
    tool_count: 0,
    refreshing: false,
    starting: false,
    stopped: false,
    url: "https://mcp.notion.com/mcp",
    disconnect_reason: null,
    ...overrides,
  };
}

const t = (key: string) => key;

describe("upstreamStatusLabel", () => {
  it("shows Stopped for a sign-in MCP stopped with its sign-in kept", () => {
    const u = makeSummary({ stopped: true, slot_owner: "alice@co.com" });
    expect(upstreamStatusLabel(u, null, t)).toBe("status.stopped");
  });

  it("shows Stopped for a sign-in MCP stopped with no sign-in kept", () => {
    const u = makeSummary({ stopped: true });
    expect(upstreamStatusLabel(u, null, t)).toBe("status.stopped");
  });

  it("asks for authentication on a running MCP with no admin sign-in", () => {
    const u = makeSummary({ stopped: false });
    expect(upstreamStatusLabel(u, null, t)).toBe("status.authenticationNeeded");
  });
});
