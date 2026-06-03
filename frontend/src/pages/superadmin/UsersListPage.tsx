import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { fetchSuperadminUsers } from "../../api/superadmin";
import type { SuperadminUserListResponse } from "../../api/types";

export function UsersListPage() {
  const { data, isLoading, error } = useQuery<SuperadminUserListResponse>({
    queryKey: ["superadmin", "users"],
    queryFn: fetchSuperadminUsers,
    refetchInterval: 60_000,
  });
  const [filter, setFilter] = useState("");

  const rows = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return data.users;
    return data.users.filter(
      (u) =>
        u.email.toLowerCase().includes(q) ||
        u.roles.some((r) => r.toLowerCase().includes(q)),
    );
  }, [data, filter]);

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load users:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Users</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {data.users.length} distinct emails across all organizations.
        </p>
      </div>

      <input
        type="search"
        placeholder="Filter by email or role…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="w-full max-w-sm rounded border border-zinc-300 bg-white px-3 py-1.5 text-sm placeholder:text-zinc-400 focus:border-zinc-500 focus:outline-none"
      />

      <div className="overflow-hidden rounded border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
              <th className="px-3 py-2">Email</th>
              <th className="px-3 py-2 text-right">Orgs</th>
              <th className="px-3 py-2">Roles</th>
              <th className="px-3 py-2">Flags</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.map((u) => (
              <tr key={u.email} className="hover:bg-zinc-50">
                <td className="px-3 py-2">
                  <Link
                    to={`/superadmin/users/${encodeURIComponent(u.email)}`}
                    className="text-zinc-900 hover:underline"
                  >
                    {u.email}
                  </Link>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {u.org_count}
                </td>
                <td className="px-3 py-2 text-zinc-700">
                  {u.roles.length === 0 ? (
                    <span className="text-zinc-400">—</span>
                  ) : (
                    u.roles.join(", ")
                  )}
                </td>
                <td className="px-3 py-2">
                  {u.is_superadmin && (
                    <span className="rounded bg-purple-100 px-1.5 py-0.5 text-xs font-medium text-purple-700">
                      superadmin
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-zinc-500">
                  {filter ? "No users match." : "No users."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
