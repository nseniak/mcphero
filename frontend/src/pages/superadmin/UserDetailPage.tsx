import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import {
  fetchSuperadminUser,
  revokeSuperadminUserSessions,
} from "../../api/superadmin";
import type {
  SuperadminSessionsRevokedResponse,
  SuperadminUserDetailResponse,
} from "../../api/types";

function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch {
    return iso;
  }
}

export function UserDetailPage() {
  const { email = "" } = useParams<{ email: string }>();
  const queryClient = useQueryClient();
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const { data, isLoading, error } = useQuery<SuperadminUserDetailResponse>({
    queryKey: ["superadmin", "user", email],
    queryFn: () => fetchSuperadminUser(email),
    enabled: !!email,
  });

  const revokeMutation = useMutation<SuperadminSessionsRevokedResponse>({
    mutationFn: () => revokeSuperadminUserSessions(email),
    onSuccess: (r) => {
      setActionMessage(
        `Revoked ${r.gateway_tokens_revoked} gateway token(s); terminated ${r.upstream_sessions_terminated} session(s) across ${r.orgs_touched} org(s).`,
      );
      queryClient.invalidateQueries({ queryKey: ["superadmin", "user", email] });
    },
    onError: (e) => {
      setActionMessage(
        `Failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    },
  });

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load user:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <Link
          to="/superadmin/users"
          className="text-xs text-zinc-500 hover:text-zinc-700"
        >
          ← All users
        </Link>
        <h1 className="mt-1 text-2xl font-semibold text-zinc-900">
          {data.email}
        </h1>
        {data.is_superadmin && (
          <span className="mt-2 inline-block rounded bg-purple-100 px-1.5 py-0.5 text-xs font-medium text-purple-700">
            superadmin
          </span>
        )}
      </div>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Actions
        </h2>
        <div className="rounded border border-zinc-200 bg-white p-4 space-y-2">
          <button
            type="button"
            onClick={() => {
              if (
                window.confirm(
                  `Revoke all gateway tokens and active sessions for ${email}? They'll need to re-authenticate their MCP clients.`,
                )
              ) {
                revokeMutation.mutate();
              }
            }}
            disabled={revokeMutation.isPending}
            className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            {revokeMutation.isPending
              ? "Revoking…"
              : "Revoke all sessions & gateway tokens"}
          </button>
          {actionMessage && (
            <div className="text-xs text-zinc-700">{actionMessage}</div>
          )}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Memberships ({data.memberships.length})
        </h2>
        <div className="overflow-hidden rounded border border-zinc-200 bg-white">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                <th className="px-3 py-2">Org</th>
                <th className="px-3 py-2">Role</th>
                <th className="px-3 py-2">Joined</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {data.memberships.map((m) => (
                <tr key={m.org_id} className="hover:bg-zinc-50">
                  <td className="px-3 py-2">
                    <Link
                      to={`/orgs/${encodeURIComponent(m.org_slug)}/admin/upstream?via=superadmin`}
                      className="text-zinc-900 hover:underline"
                    >
                      {m.org_display_name}
                    </Link>
                    <span className="ml-2 font-mono text-xs text-zinc-400">
                      {m.org_slug}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-zinc-700">{m.role}</td>
                  <td className="px-3 py-2 text-zinc-500 tabular-nums">
                    {formatDateTime(m.joined_at)}
                  </td>
                </tr>
              ))}
              {data.memberships.length === 0 && (
                <tr>
                  <td colSpan={3} className="px-3 py-6 text-center text-zinc-500">
                    No org memberships.
                    {data.is_superadmin &&
                      " (Superadmin without explicit memberships.)"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
