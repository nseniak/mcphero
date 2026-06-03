import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { fetchSuperadminOverview } from "../../api/superadmin";
import type { SuperadminOverviewResponse } from "../../api/types";

function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: number | string;
  hint?: string;
}) {
  return (
    <div className="rounded border border-zinc-200 bg-white p-4">
      <div className="text-xs font-medium uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold text-zinc-900 tabular-nums">
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-zinc-500">{hint}</div>}
    </div>
  );
}

function SystemRow({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean;
  detail?: string;
}) {
  return (
    <div className="flex items-center justify-between py-2 text-sm">
      <span className="text-zinc-700">{label}</span>
      <span className="flex items-center gap-2">
        {detail && <span className="text-xs text-zinc-500">{detail}</span>}
        <span
          className={`inline-flex h-2 w-2 rounded-full ${
            ok ? "bg-green-500" : "bg-zinc-300"
          }`}
        />
      </span>
    </div>
  );
}

export function OverviewPage() {
  const { data, isLoading, error } = useQuery<SuperadminOverviewResponse>({
    queryKey: ["superadmin", "overview"],
    queryFn: fetchSuperadminOverview,
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return <div className="text-sm text-zinc-500">Loading…</div>;
  }
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load overview:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  const { counts, system } = data;
  const runtimesHint =
    counts.runtimes_loaded < counts.orgs
      ? `${counts.runtimes_loaded} of ${counts.orgs} loaded`
      : undefined;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">Superadmin</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Cross-org overview. Mode: <span className="font-medium">{system.mode}</span>.
        </p>
      </div>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Counts
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Tile label="Organizations" value={counts.orgs} />
          <Tile label="Users" value={counts.users} hint="distinct emails" />
          <Tile
            label="Upstreams"
            value={counts.upstreams}
            hint={runtimesHint}
          />
          <Tile
            label="Live sessions"
            value={counts.upstreams_connected}
            hint={`of ${counts.upstreams}`}
          />
          <Tile label="Runtimes loaded" value={counts.runtimes_loaded} />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          System
        </h2>
        <div className="rounded border border-zinc-200 bg-white px-4 divide-y divide-zinc-100">
          <SystemRow label="Deploy mode" ok detail={system.mode} />
          <SystemRow
            label="Sentry"
            ok={system.sentry_enabled}
            detail={system.sentry_enabled ? "enabled" : "disabled"}
          />
          <SystemRow
            label="Mixpanel"
            ok={system.mixpanel_enabled}
            detail={system.mixpanel_enabled ? "enabled" : "disabled"}
          />
          <SystemRow
            label="Sandbox runner"
            ok={system.sandbox_runner_configured}
            detail={
              system.sandbox_runner_configured
                ? `${system.sandbox_runner_url_count} URL${
                    system.sandbox_runner_url_count === 1 ? "" : "s"
                  }`
                : "unsafe local-subprocess fallback"
            }
          />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Tools
        </h2>
        <div className="rounded border border-zinc-200 bg-white p-4 text-sm">
          <Link
            to="/superadmin/test-observability"
            className="text-zinc-700 hover:text-zinc-900 hover:underline"
          >
            Observability smoke tests →
          </Link>
        </div>
      </section>
    </div>
  );
}
