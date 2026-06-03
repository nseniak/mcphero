import { useQueryClient, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  fetchSuperadminOrgs,
  updateSuperadminOrgSubscription,
} from "../../api/superadmin";
import type { SuperadminOrgListResponse } from "../../api/types";

function formatDate(iso: string): string {
  try {
    return new Date(iso).toISOString().slice(0, 10);
  } catch {
    return iso;
  }
}

export function OrgsListPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery<SuperadminOrgListResponse>({
    queryKey: ["superadmin", "orgs"],
    queryFn: fetchSuperadminOrgs,
    refetchInterval: 30_000,
  });

  async function handlePlanChange(orgId: string, plan: "free" | "team") {
    await updateSuperadminOrgSubscription(orgId, plan);
    await queryClient.invalidateQueries({ queryKey: ["superadmin", "orgs"] });
  }

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load orgs:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Organizations</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {data.orgs.length} total. Counts come from cached runtimes; rows
          marked "cold" haven't been loaded since the backend last booted.
        </p>
      </div>

      <div className="overflow-hidden rounded border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
              <th className="px-3 py-2">Slug</th>
              <th className="px-3 py-2">Display name</th>
              <th className="px-3 py-2">Created</th>
              <th className="px-3 py-2">By</th>
              <th className="px-3 py-2 text-right">Members</th>
              <th className="px-3 py-2 text-right">Upstreams</th>
              <th className="px-3 py-2 text-right">Live</th>
              <th className="px-3 py-2">Runtime</th>
              <th className="px-3 py-2">Plan</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {data.orgs.map((org) => (
              <tr key={org.id} className="hover:bg-zinc-50">
                <td className="px-3 py-2 font-mono text-xs">
                  <Link
                    to={`/orgs/${encodeURIComponent(org.slug)}/admin/upstream?via=superadmin`}
                    className="text-zinc-900 hover:underline"
                  >
                    {org.slug}
                  </Link>
                </td>
                <td className="px-3 py-2 text-zinc-700">{org.display_name}</td>
                <td className="px-3 py-2 text-zinc-500 tabular-nums">
                  {formatDate(org.created_at)}
                </td>
                <td className="px-3 py-2 text-zinc-500">
                  {org.created_by_email ?? "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  <Link
                    to={`/orgs/${encodeURIComponent(org.slug)}/admin/team?via=superadmin`}
                    className="text-zinc-900 hover:underline"
                  >
                    {org.member_count}{" "}
                    <span aria-hidden className="text-zinc-400">›</span>
                  </Link>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  <Link
                    to={`/orgs/${encodeURIComponent(org.slug)}/admin/upstream?via=superadmin`}
                    className="text-zinc-900 hover:underline"
                  >
                    {org.upstream_count}{" "}
                    <span aria-hidden className="text-zinc-400">›</span>
                  </Link>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {org.upstream_connected}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`text-xs ${
                      org.runtime_loaded ? "text-zinc-500" : "text-amber-600"
                    }`}
                  >
                    {org.runtime_loaded ? "loaded" : "cold"}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <select
                    value={org.plan}
                    data-testid="superadmin-plan-select"
                    data-org-id={org.id}
                    onChange={(e) =>
                      handlePlanChange(
                        org.id, e.target.value as "free" | "team",
                      )
                    }
                    className="text-xs border border-zinc-200 rounded px-1 py-0.5 bg-white"
                  >
                    <option value="free">Free</option>
                    <option value="team">Team</option>
                  </select>
                </td>
              </tr>
            ))}
            {data.orgs.length === 0 && (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-zinc-500">
                  No organizations.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
