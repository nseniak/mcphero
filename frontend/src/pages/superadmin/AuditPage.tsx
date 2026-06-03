import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchSuperadminAudit,
  fetchSuperadminAuditAggregates,
  type SuperadminAuditFilters,
} from "../../api/superadmin";
import type {
  SuperadminAuditAggregatesResponse,
  SuperadminAuditAggregatesTopRow,
  SuperadminAuditSearchResponse,
} from "../../api/types";

function asString(v: unknown, fallback: string = ""): string {
  return typeof v === "string" ? v : fallback;
}

function TopList({
  title,
  rows,
}: {
  title: string;
  rows: SuperadminAuditAggregatesTopRow[];
}) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wider text-zinc-500">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="text-xs text-zinc-400">no data</div>
      ) : (
        <ul className="space-y-0.5 text-sm">
          {rows.map((r) => (
            <li key={r.key} className="flex justify-between gap-2">
              <span className="truncate text-zinc-700">{r.key}</span>
              <span className="tabular-nums text-zinc-500">{r.count}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function AuditPage() {
  const [filters, setFilters] = useState<SuperadminAuditFilters>({});
  const [pendingFilters, setPendingFilters] = useState<SuperadminAuditFilters>(
    {},
  );

  const search = useQuery<SuperadminAuditSearchResponse>({
    queryKey: ["superadmin", "audit", filters],
    queryFn: () => fetchSuperadminAudit(filters, 200, 0),
  });
  const aggregates = useQuery<SuperadminAuditAggregatesResponse>({
    queryKey: ["superadmin", "audit-aggregates", filters],
    queryFn: () => fetchSuperadminAuditAggregates(filters, 2000),
  });

  const setF = (k: keyof SuperadminAuditFilters, v: string) =>
    setPendingFilters((p) => ({ ...p, [k]: v || undefined }));

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Audit log</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Cross-org. Most recent first.
        </p>
      </div>

      <form
        className="flex flex-wrap gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters(pendingFilters);
        }}
      >
        <input
          type="text"
          placeholder="org_id"
          value={pendingFilters.org_id ?? ""}
          onChange={(e) => setF("org_id", e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        />
        <input
          type="text"
          placeholder="user (email)"
          value={pendingFilters.user_id ?? ""}
          onChange={(e) => setF("user_id", e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        />
        <input
          type="text"
          placeholder="upstream id"
          value={pendingFilters.upstream_id ?? ""}
          onChange={(e) => setF("upstream_id", e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        />
        <input
          type="text"
          placeholder="tool (substring)"
          value={pendingFilters.tool ?? ""}
          onChange={(e) => setF("tool", e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        />
        <input
          type="text"
          placeholder="action"
          value={pendingFilters.action ?? ""}
          onChange={(e) => setF("action", e.target.value)}
          className="rounded border border-zinc-300 bg-white px-2 py-1 text-sm"
        />
        <button
          type="submit"
          className="rounded bg-zinc-900 px-3 py-1 text-sm text-white hover:bg-zinc-800"
        >
          Search
        </button>
        <button
          type="button"
          onClick={() => {
            setPendingFilters({});
            setFilters({});
          }}
          className="rounded border border-zinc-300 bg-white px-3 py-1 text-sm hover:bg-zinc-50"
        >
          Clear
        </button>
      </form>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="rounded border border-zinc-200 bg-white p-3">
          {aggregates.data ? (
            <TopList title="Top tools" rows={aggregates.data.top_tools} />
          ) : (
            <div className="text-xs text-zinc-400">…</div>
          )}
        </div>
        <div className="rounded border border-zinc-200 bg-white p-3">
          {aggregates.data ? (
            <TopList title="Top orgs" rows={aggregates.data.top_orgs} />
          ) : (
            <div className="text-xs text-zinc-400">…</div>
          )}
        </div>
        <div className="rounded border border-zinc-200 bg-white p-3">
          {aggregates.data ? (
            <TopList
              title="Top deny rules"
              rows={aggregates.data.top_deny_rules}
            />
          ) : (
            <div className="text-xs text-zinc-400">…</div>
          )}
        </div>
      </section>

      {aggregates.data && (
        <div className="text-xs text-zinc-500">
          Latency p50: {aggregates.data.latency_p50_ms?.toFixed(0) ?? "—"} ms ·
          p95: {aggregates.data.latency_p95_ms?.toFixed(0) ?? "—"} ms · sample
          {" "}
          {aggregates.data.sample_size}
        </div>
      )}

      <div className="overflow-hidden rounded border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 bg-zinc-50 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
              <th className="px-3 py-2">When</th>
              <th className="px-3 py-2">Org</th>
              <th className="px-3 py-2">User</th>
              <th className="px-3 py-2">Upstream</th>
              <th className="px-3 py-2">Tool</th>
              <th className="px-3 py-2">Decision</th>
              <th className="px-3 py-2 text-right">Status</th>
              <th className="px-3 py-2 text-right">Latency</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {search.isLoading ? (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-zinc-400">
                  Loading…
                </td>
              </tr>
            ) : search.error ? (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-red-600">
                  {search.error instanceof Error
                    ? search.error.message
                    : "Failed to load audit log"}
                </td>
              </tr>
            ) : search.data && search.data.entries.length > 0 ? (
              search.data.entries.map((e, i) => {
                const decision = asString(e.policy_decision);
                const lat = e.latency_ms;
                return (
                  <tr key={i} className="hover:bg-zinc-50">
                    <td className="px-3 py-2 font-mono text-xs text-zinc-500">
                      {asString(e.timestamp).slice(0, 19).replace("T", " ")}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {asString(e.org_id, "—")}
                    </td>
                    <td className="px-3 py-2">{asString(e.user_id, "—")}</td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {asString(e.upstream_id, "—")}
                    </td>
                    <td className="px-3 py-2">{asString(e.tool, "—")}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`text-xs ${
                          decision === "deny"
                            ? "text-red-700"
                            : decision === "allow"
                              ? "text-green-700"
                              : "text-zinc-500"
                        }`}
                      >
                        {decision || "—"}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {asString(e.response_status, "—")}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-zinc-500">
                      {typeof lat === "number" ? `${Math.round(lat)} ms` : "—"}
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-zinc-500">
                  No entries match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
