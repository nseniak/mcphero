import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { fetchSuperadminUpstreams } from "../../api/superadmin";
import type { SuperadminUpstreamListResponse } from "../../api/types";

export function UpstreamsListPage() {
  const { data, isLoading, error } = useQuery<SuperadminUpstreamListResponse>({
    queryKey: ["superadmin", "upstreams"],
    queryFn: fetchSuperadminUpstreams,
    refetchInterval: 30_000,
  });
  const [transport, setTransport] = useState<string>("");
  const [authMode, setAuthMode] = useState<string>("");
  const [status, setStatus] = useState<string>("");

  const rows = useMemo(() => {
    if (!data) return [];
    return data.upstreams.filter(
      (u) =>
        (!transport || u.transport === transport) &&
        (!authMode || u.auth_mode === authMode) &&
        (!status ||
          (status === "connected" && u.connected) ||
          (status === "disconnected" && !u.connected)),
    );
  }, [data, transport, authMode, status]);

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load upstreams:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  const partial = data.runtimes_loaded < data.runtimes_total;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Upstreams</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {data.upstreams.length} upstream{data.upstreams.length === 1 ? "" : "s"}{" "}
          across {data.runtimes_loaded} loaded runtime
          {data.runtimes_loaded === 1 ? "" : "s"}.
          {partial && (
            <span className="ml-2 text-amber-600">
              {data.runtimes_total - data.runtimes_loaded} runtime(s) not loaded — counts may be
              incomplete.
            </span>
          )}
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <select
          value={transport}
          onChange={(e) => setTransport(e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        >
          <option value="">All transports</option>
          <option value="stdio">stdio</option>
          <option value="streamable_http">HTTP</option>
        </select>
        <select
          value={authMode}
          onChange={(e) => setAuthMode(e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        >
          <option value="">All auth modes</option>
          <option value="service_account">service_account</option>
          <option value="admin_oauth">admin_oauth</option>
          <option value="per_user_oauth">per_user_oauth</option>
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        >
          <option value="">Any</option>
          <option value="connected">Live</option>
          <option value="disconnected">Not live</option>
        </select>
      </div>

      <div className="overflow-hidden rounded border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
              <th className="px-3 py-2">Org</th>
              <th className="px-3 py-2">Upstream</th>
              <th className="px-3 py-2">Transport</th>
              <th className="px-3 py-2">Auth</th>
              <th className="px-3 py-2">Live</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.map((u) => (
              <tr key={`${u.org_id}:${u.upstream_id}`} className="hover:bg-zinc-50">
                <td className="px-3 py-2">
                  <Link
                    to={`/orgs/${encodeURIComponent(u.org_slug)}/admin/upstream?via=superadmin`}
                    className="text-zinc-900 hover:underline"
                  >
                    {u.org_display_name}
                  </Link>
                  <span className="ml-2 font-mono text-xs text-zinc-400">
                    {u.org_slug}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <Link
                    to={`/orgs/${encodeURIComponent(u.org_slug)}/admin/upstream/${encodeURIComponent(u.upstream_id)}?via=superadmin`}
                    className="text-zinc-900 hover:underline"
                  >
                    {u.display_name}
                  </Link>
                  <span className="ml-2 font-mono text-xs text-zinc-400">
                    {u.upstream_id}
                  </span>
                </td>
                <td className="px-3 py-2 text-zinc-500">{u.transport}</td>
                <td className="px-3 py-2 text-zinc-500">{u.auth_mode}</td>
                <td className="px-3 py-2">
                  <span
                    role="img"
                    aria-label={
                      u.connected ? "live runtime session" : "no live session"
                    }
                    title={
                      u.connected ? "live runtime session" : "no live session"
                    }
                    className={`inline-flex h-2 w-2 rounded-full ${
                      u.connected ? "bg-green-500" : "bg-zinc-300"
                    }`}
                  />
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-zinc-500">
                  No upstreams match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
