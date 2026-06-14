import { Link } from "react-router";
import { ChevronRight } from "lucide-react";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import type {
  RoleAccessInfo,
  ToolAccessConfig,
  ToolInfo,
  UpstreamSummary,
} from "../../api/types";

/** Annotation hint key → human-readable label */
export const ANNOTATION_LABELS: Record<string, string> = {
  readOnly: "Read-only",
  destructive: "Destructive",
  idempotent: "Idempotent",
  openWorld: "Open-world",
};

/** Convert ToolInfo annotations to flag dict matching policy keys */
export function toolFlags(tool: ToolInfo): Record<string, boolean> {
  const flags: Record<string, boolean> = {};
  const a = tool.annotations;
  if (!a) return flags;
  if (a.readOnlyHint != null) flags.readOnly = a.readOnlyHint;
  if (a.destructiveHint != null) flags.destructive = a.destructiveHint;
  if (a.idempotentHint != null) flags.idempotent = a.idempotentHint;
  if (a.openWorldHint != null) flags.openWorld = a.openWorldHint;
  return flags;
}

/** Get the set of annotation keys present on any tool in a list */
export function presentAnnotations(tools: ToolInfo[]): string[] {
  const keys = new Set<string>();
  for (const t of tools) {
    for (const [k, v] of Object.entries(toolFlags(t))) {
      if (v) keys.add(k);
    }
  }
  return [...keys].sort();
}

/** Compute the effective access for a tool given a ToolAccessConfig (mirrors backend logic). */
export function resolveToolDefault(
  config: ToolAccessConfig | null | undefined,
  flags: Record<string, boolean>,
): boolean {
  if (!config) return true; // no config = all allowed

  // Check category defaults (deny wins)
  if (config.category_defaults && Object.keys(config.category_defaults).length > 0) {
    const matched: boolean[] = [];
    for (const [annKey, annValue] of Object.entries(flags)) {
      if (annKey in config.category_defaults && annValue) {
        matched.push(config.category_defaults[annKey]);
      }
    }
    if (matched.length > 0) {
      return matched.every(Boolean); // deny wins
    }
  }

  // Fall back to fallback_enabled (null = per-tool mode, deny unknown tools)
  return config.fallback_enabled ?? false;
}

export function AnnotationBadge({ annotation, value }: { annotation: string; value: boolean }) {
  const label = ANNOTATION_LABELS[annotation] ?? annotation;
  const color = value
    ? "bg-blue-50 text-blue-600 border-blue-200"
    : "bg-zinc-50 text-zinc-400 border-zinc-200";
  return (
    <span className={`px-1.5 py-0.5 text-[10px] rounded border ${color}`}>
      {label}
    </span>
  );
}

export function ToolAccessSection({
  role,
  upstreams,
  allTools,
}: {
  role: RoleAccessInfo;
  upstreams: UpstreamSummary[];
  allTools: ToolInfo[];
}) {
  const orgSlug = useOrgSlug();
  // Group tools by upstream
  const toolsByUpstream = new Map<string, ToolInfo[]>();
  for (const t of allTools) {
    const list = toolsByUpstream.get(t.upstream_id) ?? [];
    list.push(t);
    toolsByUpstream.set(t.upstream_id, list);
  }

  // Only show upstreams that have tools
  const upstreamsWithTools = upstreams.filter((u) => (toolsByUpstream.get(u.id)?.length ?? 0) > 0);

  if (upstreamsWithTools.length === 0) return null;

  return (
    <div className="mt-6">
      <h3 className="text-lg font-medium text-zinc-900 mb-3">Tool Permissions</h3>
      <div className="border border-zinc-200 rounded-lg overflow-hidden divide-y divide-zinc-100">
        {upstreamsWithTools.map((upstream) => {
          const toolCount = toolsByUpstream.get(upstream.id)?.length ?? 0;
          return (
            <Link
              key={upstream.id}
              to={`/orgs/${orgSlug}/admin/permissions/tools?role=${encodeURIComponent(role.name)}&upstream=${encodeURIComponent(upstream.id)}`}
              className="flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 transition-colors"
            >
              <span>{upstream.display_name}</span>
              <span className="text-zinc-400 text-xs ml-auto">{toolCount} tools</span>
              <ChevronRight size={14} className="text-zinc-400" />
            </Link>
          );
        })}
      </div>
    </div>
  );
}
