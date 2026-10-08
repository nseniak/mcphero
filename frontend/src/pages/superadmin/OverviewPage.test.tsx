import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { OverviewPage } from "./OverviewPage";
import type { SuperadminOverviewResponse } from "../../api/types";

vi.mock("../../api/superadmin", () => ({
  fetchSuperadminOverview: vi.fn(),
}));
vi.mock("../../hooks/useFeatures", () => ({ useFeatures: vi.fn() }));

import { fetchSuperadminOverview } from "../../api/superadmin";
import { useFeatures } from "../../hooks/useFeatures";

function makeOverview(): SuperadminOverviewResponse {
  return {
    counts: {
      orgs: 1,
      users: 1,
      upstreams: 0,
      upstreams_connected: 0,
      runtimes_loaded: 1,
    },
    system: { mode: "cloud", mixpanel_enabled: false, sentry_enabled: false },
  };
}

function renderWithSandbox(sandboxProvider: "e2b" | "local-subprocess") {
  vi.mocked(fetchSuperadminOverview).mockResolvedValue(makeOverview());
  vi.mocked(useFeatures).mockReturnValue({
    allowStdioMcp: true,
    mode: "cloud",
    sandboxProvider,
    isLoading: false,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OverviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.clearAllMocks());

// The row used to read fields the backend stopped sending when the
// own-runner was removed, so it always claimed the unsafe fallback.
it("shows E2B when the backend runs stdio MCPs on E2B", async () => {
  renderWithSandbox("e2b");
  expect(await screen.findByText("Sandbox")).toBeTruthy();
  expect(screen.getByText("E2B")).toBeTruthy();
  expect(screen.queryByText(/local.subprocess/)).toBeNull();
});

it("flags the unsafe local subprocess path", async () => {
  renderWithSandbox("local-subprocess");
  expect(await screen.findByText("local subprocess (unsafe)")).toBeTruthy();
  expect(screen.queryByText("E2B")).toBeNull();
});
