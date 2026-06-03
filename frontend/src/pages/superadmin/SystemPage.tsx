import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { fetchSuperadminSystem } from "../../api/superadmin";
import type { SuperadminSystemResponse } from "../../api/types";

function Row({
  label,
  value,
  ok,
}: {
  label: string;
  value: React.ReactNode;
  ok?: boolean;
}) {
  return (
    <div className="flex items-center justify-between border-b border-zinc-100 py-2 text-sm last:border-b-0">
      <span className="text-zinc-700">{label}</span>
      <span className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-500">{value}</span>
        {ok !== undefined && (
          <span
            className={`inline-flex h-2 w-2 rounded-full ${
              ok ? "bg-green-500" : "bg-zinc-300"
            }`}
          />
        )}
      </span>
    </div>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
        {title}
      </h2>
      <div className="rounded border border-zinc-200 bg-white px-4">
        {children}
      </div>
    </section>
  );
}

export function SystemPage() {
  const { data, isLoading, error } = useQuery<SuperadminSystemResponse>({
    queryKey: ["superadmin", "system"],
    queryFn: fetchSuperadminSystem,
    refetchInterval: 60_000,
  });

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error || !data) {
    return (
      <div className="text-sm text-red-600">
        Failed to load system info:{" "}
        {error instanceof Error ? error.message : "unknown error"}
      </div>
    );
  }

  const b = data.backend;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-900">System</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Backend deployment info. Secrets are never returned — only "set"
          flags and an encryption-key fingerprint (first 8 hex of SHA-256).
        </p>
      </div>

      <Card title="Deploy">
        <Row label="Mode" value={b.mode} />
        <Row label="Release" value={b.release || "—"} />
        <Row label="Allow stdio MCPs" value={b.allow_stdio_mcp ? "yes" : "no"} />
        <Row label="Test mode" value={b.test_mode ? "on" : "off"} ok={!b.test_mode} />
      </Card>

      <Card title="Storage">
        <Row label="Mongo URI" value={b.mongo_uri_set ? "set" : "—"} ok={b.mongo_uri_set} />
        <Row label="Mongo DB" value={b.mongo_db_name || "—"} />
        <Row label="Redis URL" value={b.redis_url_set ? "set" : "—"} ok={b.redis_url_set} />
        <Row
          label="Encryption key"
          value={
            b.encryption_key_set
              ? `set · fp ${b.encryption_key_fingerprint}`
              : "—"
          }
          ok={b.encryption_key_set}
        />
      </Card>

      <Card title="Telemetry">
        <Row
          label="Sentry"
          value={
            b.sentry_dsn_set
              ? `enabled · ${b.sentry_environment || "default"}`
              : "disabled"
          }
          ok={b.sentry_dsn_set}
        />
        <Row
          label="Mixpanel"
          value={
            b.mixpanel_token_set
              ? `enabled · ${b.mixpanel_api_host || "us"}`
              : "disabled"
          }
          ok={b.mixpanel_token_set}
        />
      </Card>

      <Card title="Diagnostics">
        <div className="py-3 text-sm">
          <Link
            to="/superadmin/test-observability"
            className="text-zinc-700 hover:text-zinc-900 hover:underline"
          >
            Observability smoke tests →
          </Link>
        </div>
      </Card>
    </div>
  );
}
