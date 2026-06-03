import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  fetchSuperadminAuthHealth,
  triggerSuperadminReauth,
} from "../../api/superadmin";
import type {
  SuperadminAuthHealthResponse,
  SuperadminReauthResponse,
} from "../../api/types";

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "neutral" | "amber" | "red";
}) {
  const toneClass =
    tone === "red"
      ? "text-red-700"
      : tone === "amber"
        ? "text-amber-700"
        : "text-zinc-900";
  return (
    <div className="rounded border border-zinc-200 bg-white p-4">
      <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass}`}>
        {value}
      </div>
    </div>
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
  } catch {
    return iso;
  }
}

type ReauthVars = { email: string; orgId: string; upstreamId: string };

export function AuthHealthPage() {
  const queryClient = useQueryClient();
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const { data, isLoading, error } = useQuery<SuperadminAuthHealthResponse>({
    queryKey: ["superadmin", "auth-health"],
    queryFn: fetchSuperadminAuthHealth,
    refetchInterval: 60_000,
  });

  const reauthMutation = useMutation<SuperadminReauthResponse, Error, ReauthVars>(
    {
      mutationFn: (v) => triggerSuperadminReauth(v.email, v.orgId, v.upstreamId),
      onSuccess: (r) => {
        setActionMessage(
          `Cleared OAuth token for ${r.email} on ${r.upstream_id} (${r.org_id}). The next request will re-auth.`,
        );
        queryClient.invalidateQueries({
          queryKey: ["superadmin", "auth-health"],
        });
      },
      onError: (e) => {
        setActionMessage(`Re-auth failed: ${e.message}`);
      },
    },
  );

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load auth health:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  const partial = data.runtimes_loaded < data.runtimes_total;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Auth health</h1>
        <p className="mt-1 text-sm text-zinc-500">
          OAuth tokens and connection errors across loaded runtimes.
          {partial && (
            <span className="ml-2 text-amber-600">
              {data.runtimes_total - data.runtimes_loaded} runtime(s) not loaded —
              counts may be incomplete.
            </span>
          )}
        </p>
      </div>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Summary
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Tile label="Total tokens" value={data.total_tokens} tone="neutral" />
          <Tile label="Expired" value={data.expired} tone="red" />
          <Tile label="Expiring 24h" value={data.expiring_soon} tone="amber" />
          <Tile label="Refresh failures" value={data.failed_refresh} tone="red" />
        </div>
      </section>

      {actionMessage && (
        <div className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700">
          {actionMessage}
        </div>
      )}

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Tokens (worst first)
        </h2>
        <div className="overflow-hidden rounded border border-zinc-200 bg-white">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                <th className="px-3 py-2">Org</th>
                <th className="px-3 py-2">Upstream</th>
                <th className="px-3 py-2">User</th>
                <th className="px-3 py-2">Expires</th>
                <th className="px-3 py-2 text-right">Failures</th>
                <th className="px-3 py-2">Last failure</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {data.rows.map((r) => (
                <tr
                  key={`${r.org_id}:${r.upstream_id}:${r.user_id}`}
                  className="hover:bg-zinc-50"
                >
                  <td className="px-3 py-2 font-mono text-xs">
                    <Link
                      to={`/orgs/${encodeURIComponent(r.org_slug)}/admin/upstream?via=superadmin`}
                      className="text-zinc-900 hover:underline"
                    >
                      {r.org_slug}
                    </Link>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {r.upstream_id}
                  </td>
                  <td className="px-3 py-2">
                    {r.is_admin_token ? (
                      <span className="text-zinc-500">admin token</span>
                    ) : (
                      <Link
                        to={`/superadmin/users/${encodeURIComponent(r.user_id)}`}
                        className="text-zinc-900 hover:underline"
                      >
                        {r.user_id}
                      </Link>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-zinc-500">
                    {formatDate(r.expires_at)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {r.refresh_failures > 0 ? (
                      <span className="text-red-700">{r.refresh_failures}</span>
                    ) : (
                      <span className="text-zinc-400">0</span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-zinc-500">
                    {formatDate(r.last_failure_at)}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {r.expired && (
                      <span className="rounded bg-red-100 px-1 text-red-700">
                        expired
                      </span>
                    )}
                    {r.expiring_soon && (
                      <span className="ml-1 rounded bg-amber-100 px-1 text-amber-700">
                        soon
                      </span>
                    )}
                    {!r.expired && !r.expiring_soon && r.refresh_failures === 0 && (
                      <span className="text-zinc-400">ok</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {!r.is_admin_token && (
                      <button
                        type="button"
                        onClick={() => {
                          if (
                            window.confirm(
                              `Clear ${r.user_id}'s OAuth token for ${r.upstream_id} in ${r.org_slug}? Their next request will re-authenticate.`,
                            )
                          ) {
                            reauthMutation.mutate({
                              email: r.user_id,
                              orgId: r.org_id,
                              upstreamId: r.upstream_id,
                            });
                          }
                        }}
                        disabled={reauthMutation.isPending}
                        className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-xs hover:bg-zinc-50 disabled:opacity-50"
                      >
                        Re-auth
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {data.rows.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-center text-zinc-500">
                    No stored tokens.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Connection errors ({data.connection_errors.length})
        </h2>
        <div className="overflow-hidden rounded border border-zinc-200 bg-white">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                <th className="px-3 py-2">Org</th>
                <th className="px-3 py-2">Upstream</th>
                <th className="px-3 py-2">Error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {data.connection_errors.map((e) => (
                <tr
                  key={`${e.org_id}:${e.upstream_id}`}
                  className="hover:bg-zinc-50"
                >
                  <td className="px-3 py-2 font-mono text-xs">{e.org_slug}</td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {e.upstream_id}
                  </td>
                  <td className="px-3 py-2 text-zinc-700">{e.error}</td>
                </tr>
              ))}
              {data.connection_errors.length === 0 && (
                <tr>
                  <td colSpan={3} className="px-3 py-6 text-center text-zinc-500">
                    No connection errors.
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
