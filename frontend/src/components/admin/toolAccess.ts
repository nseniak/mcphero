import type { ToolAccessConfig, ToolInfo } from "../../api/types";

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
