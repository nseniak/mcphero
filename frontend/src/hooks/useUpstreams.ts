import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchUpstreams, refreshUpstreamStatus } from "../api/admin";
import type { UpstreamSummary } from "../api/types";
import { useOrgSlug } from "./useOrgSlug";

export function useUpstreams(opts: { enabled?: boolean } = {}) {
  const { enabled = true } = opts;
  const queryClient = useQueryClient();
  // Scope the cache by org slug: a super-admin drilling from one org's
  // upstream list to another's (``/orgs/{slug}/admin/upstream``) must
  // not be served the previous org's cached rows on first paint. The
  // request itself is already org-scoped (``apiFetch`` sends the route
  // slug as ``X-Org-Slug``); without the slug in the key react-query
  // hands back stale cross-org data until a manual refresh.
  const orgSlug = useOrgSlug();
  const queryKey = ["upstreams", orgSlug] as const;

  const { data: upstreams = [], isLoading } = useQuery<UpstreamSummary[]>({
    queryKey,
    queryFn: fetchUpstreams,
    enabled,
  });

  const connectedCount = upstreams.filter((u) => u.ready).length;
  const disconnectedCount = upstreams.filter((u) => !u.ready && !u.disconnect_reason).length;
  const errorCount = upstreams.filter((u) => !u.ready && u.disconnect_reason).length;
  // Per-transport counts back the sidebar's capacity meter and the
  // pre-flight Add-MCP gate.
  const httpCount = upstreams.filter(
    (u) => u.transport === "streamable_http",
  ).length;
  const stdioCount = upstreams.filter((u) => u.transport === "stdio").length;

  const refresh = async () => {
    const updated = await refreshUpstreamStatus();
    queryClient.setQueryData(queryKey, updated);
  };

  const setUpstreams = (updater: UpstreamSummary[] | ((prev: UpstreamSummary[]) => UpstreamSummary[])) => {
    queryClient.setQueryData<UpstreamSummary[]>(queryKey, (prev) => {
      if (typeof updater === "function") return updater(prev ?? []);
      return updater;
    });
  };

  const invalidate = () => queryClient.invalidateQueries({ queryKey });

  return { upstreams, isLoading, connectedCount, disconnectedCount, errorCount, httpCount, stdioCount, refresh, setUpstreams, invalidate };
}
