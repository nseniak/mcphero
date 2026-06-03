import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import type { ReactNode } from "react";

import { useUpstreams } from "./useUpstreams";
import type { UpstreamSummary } from "../api/types";

vi.mock("../api/admin", () => ({
  fetchUpstreams: vi.fn(),
  refreshUpstreamStatus: vi.fn(),
}));
// useUpstreams -> useOrgSlug -> useAuth. The route slug is what we test;
// the user fallback isn't exercised here, so a null user is fine.
vi.mock("./useAuth", () => ({ useAuth: () => ({ user: null }) }));

import { fetchUpstreams } from "../api/admin";

function makeSummary(id: string): UpstreamSummary {
  return {
    id,
    display_name: id,
    transport: "streamable_http",
    auth_mode: "service_account",
    ready: true,
    slot_owner: null,
    tool_count: 0,
    refreshing: false,
    starting: false,
    url: "http://localhost/mcp",
    disconnect_reason: null,
  };
}

function makeClient(): QueryClient {
  // staleTime Infinity mirrors prod's non-zero default: an existing
  // cache entry is served without refetch — which is exactly what made
  // the old bare-key version show the previous org's rows.
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
}

function wrapperFor(qc: QueryClient, slug: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/orgs/${slug}/admin/upstream`]}>
          <Routes>
            <Route
              path="/orgs/:slug/admin/upstream"
              element={<>{children}</>}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

beforeEach(() => {
  vi.mocked(fetchUpstreams).mockResolvedValue([makeSummary("a")]);
});

afterEach(() => vi.clearAllMocks());

describe("useUpstreams cache scoping by org slug", () => {
  it("keys the query by the route org slug, not a bare key", async () => {
    const qc = makeClient();
    const { result } = renderHook(() => useUpstreams(), {
      wrapper: wrapperFor(qc, "acme"),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(qc.getQueryData(["upstreams", "acme"])).toBeDefined();
    // The bug: data lived under ["upstreams"] with no slug.
    expect(qc.getQueryData(["upstreams"])).toBeUndefined();
  });

  it("does not serve another org's cached list (separate cache entry)", async () => {
    const qc = makeClient();

    const first = renderHook(() => useUpstreams(), {
      wrapper: wrapperFor(qc, "acme"),
    });
    await waitFor(() => expect(first.result.current.isLoading).toBe(false));
    expect(vi.mocked(fetchUpstreams)).toHaveBeenCalledTimes(1);

    // Drilling into a different org is a cache miss → it must refetch,
    // rather than paint "acme" rows from a shared key. With the old bare
    // key + non-zero staleTime, this second mount fetched 0 times.
    const second = renderHook(() => useUpstreams(), {
      wrapper: wrapperFor(qc, "globex"),
    });
    await waitFor(() => expect(second.result.current.isLoading).toBe(false));

    expect(qc.getQueryData(["upstreams", "globex"])).toBeDefined();
    expect(vi.mocked(fetchUpstreams)).toHaveBeenCalledTimes(2);
  });
});
