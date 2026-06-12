import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router";

import { ServiceTokensPage } from "./ServiceTokensPage";
import type { ServiceTokenInfo } from "../../api/admin";

// RoleBadge renders a router <Link>; give it a context.
function renderPage() {
  return render(
    <MemoryRouter>
      <ServiceTokensPage />
    </MemoryRouter>,
  );
}

vi.mock("../../api/admin", () => ({
  listServiceTokens: vi.fn(),
  createServiceToken: vi.fn(),
  revokeServiceToken: vi.fn(),
  fetchRoles: vi.fn(),
}));
vi.mock("../../api/user", () => ({
  fetchGatewayConfig: vi.fn(),
}));

import {
  createServiceToken,
  fetchRoles,
  listServiceTokens,
  revokeServiceToken,
} from "../../api/admin";
import { fetchGatewayConfig } from "../../api/user";

function makeToken(label: string, role = "user"): ServiceTokenInfo {
  return {
    label,
    role,
    created_by: "admin@example.com",
    created_at: "2026-06-01T12:00:00+00:00",
    last_used_at: null,
  };
}

function makeRole(name: string, isAdmin = false) {
  return {
    name,
    is_admin: isAdmin,
    is_default: false,
    user_count: 0,
    service_token_count: 0,
  };
}

function mockApis({
  tokens = [] as ServiceTokenInfo[],
} = {}) {
  vi.mocked(listServiceTokens).mockResolvedValue(tokens);
  vi.mocked(fetchRoles).mockResolvedValue([
    makeRole("user"),
    makeRole("admin", true),
  ]);
  vi.mocked(fetchGatewayConfig).mockResolvedValue({
    url: "http://localhost:8080/mcp/acme",
    connected_users: [],
    all_users: [],
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ServiceTokensPage", () => {
  it("renders the empty state when no tokens exist", async () => {
    mockApis();
    renderPage();
    expect(
      await screen.findByText(/No service tokens yet/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /New token/ }),
    ).toBeInTheDocument();
  });

  it("lists existing tokens with role and never-used marker", async () => {
    mockApis({ tokens: [makeToken("ci-bot", "user")] });
    renderPage();
    expect(await screen.findByText("ci-bot")).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
    expect(screen.getByText("Never")).toBeInTheDocument();
  });

  it("create flow shows the raw token exactly once with a copy button", async () => {
    mockApis();
    vi.mocked(createServiceToken).mockResolvedValue({
      token: "svct_raw-value-shown-once",
      info: makeToken("ci-bot"),
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: /New token/ }),
    );
    await user.type(
      screen.getByPlaceholderText(/e.g. ci-bot/),
      "ci-bot",
    );
    await user.selectOptions(screen.getByRole("combobox"), "user");
    await user.click(screen.getByRole("button", { name: /Add/ }));

    await waitFor(() =>
      expect(createServiceToken).toHaveBeenCalledWith("ci-bot", "user"),
    );
    expect(
      await screen.findByTestId("service-token-value"),
    ).toHaveTextContent("svct_raw-value-shown-once");
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    // The connection snippet embeds the token + gateway URL.
    expect(
      screen.getByText(/"Authorization": "Bearer svct_raw-value-shown-once"/),
    ).toBeInTheDocument();

    // Dismiss — the raw value disappears for good.
    await user.click(screen.getByRole("button", { name: /Done/ }));
    expect(
      screen.queryByTestId("service-token-value"),
    ).not.toBeInTheDocument();
  });

  it("invalid label disables the Add button and shows the hint", async () => {
    mockApis();
    const user = userEvent.setup();
    renderPage();
    await user.click(
      await screen.findByRole("button", { name: /New token/ }),
    );
    await user.type(
      screen.getByPlaceholderText(/e.g. ci-bot/),
      "Bad Label!",
    );
    await user.selectOptions(screen.getByRole("combobox"), "user");
    expect(
      screen.getByText(/Lowercase letters, digits/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add/ })).toBeDisabled();
  });

  it("revoke goes through the confirm dialog", async () => {
    mockApis({ tokens: [makeToken("ci-bot")] });
    vi.mocked(revokeServiceToken).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("ci-bot");
    // The row's revoke trigger is the trash button (tooltip trigger).
    const triggers = screen.getAllByRole("button");
    const trash = triggers.find((b) =>
      b.querySelector("svg.lucide-trash-2, svg.lucide-trash2"),
    ) ?? triggers[triggers.length - 1];
    await user.click(trash);

    // Confirm dialog appears; confirm the revoke.
    const confirmButton = await screen.findByRole("button", {
      name: /^Revoke$/,
    });
    await user.click(confirmButton);
    await waitFor(() =>
      expect(revokeServiceToken).toHaveBeenCalledWith("ci-bot"),
    );
  });
});
