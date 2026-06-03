/**
 * Cross-reference helpers — given a parsed mcpServers JSON and the
 * list of defined env vars, work out which env vars are referenced
 * (and how often), and which references are unresolved.
 *
 * Drives the per-row "Referenced N×" badge and the top-of-section
 * unresolved-references warning + ``[Define NAME]`` button.
 */

// Same grammar as the backend substitutor: ``${NAME}`` tokens with a
// strict uppercase-or-underscore name; ``\${NAME}`` is an escape and
// must be skipped. The ``(?<!\\\\)`` look-behind keeps the helper as
// a single regex pass.
const PLACEHOLDER_RE = /(?<!\\)\$\{([A-Z_][A-Z0-9_]*)\}/g;

export interface ReferenceLocation {
  /** The MCP id this reference came from (when scanning the whole
   *  mcpServers wrapper); ``""`` when scanning a single-server form
   *  or a Sandbox-file ``target_path``. */
  mcpId: string;
  /** Which user-controlled string field carried the reference. The
   *  union mirrors the backend substitutor's scope so an unresolved
   *  ``${VAR}`` in any of them shows up in the admin's amber
   *  "undefined variables" callout. ``target_path`` covers
   *  Sandbox-file target paths, which accept the same ``${...}``
   *  references as env-var values. */
  field: "env" | "headers" | "command" | "args" | "url" | "target_path";
  /** The JSON key whose value contains the reference (``"command"``
   *  / ``"url"`` for those scalar fields; the env / headers key for
   *  those mappings; the args index as a string for that array;
   *  the Sandbox-file's ``name`` for ``target_path``). */
  jsonKey: string;
}

export interface TemplateVarReference {
  /** The placeholder name, e.g. ``GITHUB_TOKEN``. */
  name: string;
  location: ReferenceLocation;
}

export interface RefInfo {
  count: number;
  defined: boolean;
}

export interface ReferenceAnalysis {
  byName: Map<string, RefInfo>;
  unresolved: TemplateVarReference[];
}

function findInValue(
  value: unknown,
  field: ReferenceLocation["field"],
  jsonKey: string,
  mcpId: string,
  out: TemplateVarReference[],
): void {
  if (typeof value !== "string") return;
  for (const match of value.matchAll(PLACEHOLDER_RE)) {
    out.push({
      name: match[1],
      location: { mcpId, field, jsonKey },
    });
  }
}

function findInServerEntry(
  mcpId: string,
  entry: Record<string, unknown>,
  out: TemplateVarReference[],
): void {
  // Walk every user-controlled string field on the entry so an
  // unresolved ``${VAR}`` anywhere the backend substitutes shows up
  // in the admin's amber callout (and not just env / headers).
  if (typeof entry.command === "string") {
    findInValue(entry.command, "command", "command", mcpId, out);
  }
  if (Array.isArray(entry.args)) {
    (entry.args as unknown[]).forEach((v, idx) => {
      findInValue(v, "args", String(idx), mcpId, out);
    });
  }
  if (typeof entry.url === "string") {
    findInValue(entry.url, "url", "url", mcpId, out);
  }
  const env = entry.env;
  if (env && typeof env === "object") {
    for (const [k, v] of Object.entries(env as Record<string, unknown>)) {
      findInValue(v, "env", k, mcpId, out);
    }
  }
  const headers = entry.headers;
  if (headers && typeof headers === "object") {
    for (const [k, v] of Object.entries(headers as Record<string, unknown>)) {
      findInValue(v, "headers", k, mcpId, out);
    }
  }
}

function looksLikeServerEntry(obj: Record<string, unknown>): boolean {
  // ``command`` / ``url`` are the canonical server-entry markers, but
  // we also accept a bare ``env`` / ``headers`` object so partial /
  // in-progress JSON (the user is still typing) still gets walked.
  return (
    typeof obj.url === "string"
    || typeof obj.command === "string"
    || (typeof obj.env === "object" && obj.env !== null)
    || (typeof obj.headers === "object" && obj.headers !== null)
  );
}

/**
 * Walk a parsed JSON object looking for ``${NAME}`` tokens in every
 * user-controlled string field of an MCP server entry — ``command``,
 * each element of ``args``, ``url``, ``env`` values, and ``headers``
 * values. ``\${NAME}`` is treated as an escape and ignored, matching
 * the backend substitutor.
 *
 * Recognises three shapes:
 * - Full wrapper: ``{ "mcpServers": { id: { command|url, ... } } }``
 * - Single-id wrapper: ``{ id: { command|url, env, headers, ... } }``
 *   (the form the create wizard accepts and stores).
 * - Bare server entry: ``{ command|url, env, headers, ... }``.
 */
export function findReferences(
  parsedJson: unknown,
): TemplateVarReference[] {
  const out: TemplateVarReference[] = [];
  if (!parsedJson || typeof parsedJson !== "object") return out;
  const obj = parsedJson as Record<string, unknown>;

  // 1. Explicit ``mcpServers: { ... }`` wrapper.
  const wrapper = obj.mcpServers;
  if (wrapper && typeof wrapper === "object") {
    for (const [mcpId, entry] of Object.entries(
      wrapper as Record<string, unknown>,
    )) {
      if (entry && typeof entry === "object") {
        findInServerEntry(mcpId, entry as Record<string, unknown>, out);
      }
    }
    return out;
  }

  // 2. Bare server entry ({command|url, env, headers, ...}).
  if (looksLikeServerEntry(obj)) {
    findInServerEntry("", obj, out);
    return out;
  }

  // 3. Single-id wrapper ({"<id>": {command|url, ...}}). Walk every
  // top-level key whose value looks like a server entry — same logic
  // the upstream-config-loader applies on the backend.
  for (const [mcpId, entry] of Object.entries(obj)) {
    if (
      entry
      && typeof entry === "object"
      && looksLikeServerEntry(entry as Record<string, unknown>)
    ) {
      findInServerEntry(mcpId, entry as Record<string, unknown>, out);
    }
  }
  return out;
}

/** Walk a list of Sandbox files and emit a ``${NAME}`` reference per
 *  unescaped placeholder in each file's ``target_path``. ``mcpId``
 *  on the location is left empty (target_paths don't live inside
 *  the JSON config wrapper); ``jsonKey`` carries the file name so
 *  the unresolved-variable callout can point operators at the
 *  right file. */
export function findReferencesInSandboxFiles(
  files: ReadonlyArray<{ name: string; target_path: string }>,
): TemplateVarReference[] {
  const out: TemplateVarReference[] = [];
  for (const f of files) {
    findInValue(f.target_path, "target_path", f.name, "", out);
  }
  return out;
}

export function analyseReferences(
  references: TemplateVarReference[],
  definedTemplateVarNames: Iterable<string>,
): ReferenceAnalysis {
  const definedSet = new Set<string>(definedTemplateVarNames);
  const byName = new Map<string, RefInfo>();
  const unresolved: TemplateVarReference[] = [];
  for (const ref of references) {
    const existing = byName.get(ref.name) ?? {
      count: 0,
      defined: definedSet.has(ref.name),
    };
    existing.count += 1;
    byName.set(ref.name, existing);
    if (!existing.defined) unresolved.push(ref);
  }
  // Defined secrets that aren't referenced still need an entry so
  // the SecretsManager can render an "Unreferenced" badge.
  for (const name of definedSet) {
    if (!byName.has(name)) {
      byName.set(name, { count: 0, defined: true });
    }
  }
  return { byName, unresolved };
}

/** Convenience for the editor decoration extension. */
export function resolvedNamesFromAnalysis(
  analysis: ReferenceAnalysis,
): Set<string> {
  const set = new Set<string>();
  for (const [name, info] of analysis.byName) {
    if (info.defined) set.add(name);
  }
  return set;
}
