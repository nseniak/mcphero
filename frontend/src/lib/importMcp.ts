// Pure helpers for the MCP import dialog: grouping rows by project,
// stable row keys, default selection / id maps, and live id-uniqueness
// validation. Kept side-effect free so they can be unit-tested without a
// DOM (see importMcp.test.ts).
import type {
  ImportConfirmEntry,
  ImportEntry,
  ImportErrorDetail,
  ImportPreviewResponse,
  ImportResultResponse,
} from "../api/types";

export interface ImportGroup {
  label: string;
  scope: string;
  projectPath: string | null;
  entries: ImportEntry[];
}

export type IdErrorCode = "empty" | "exists" | "duplicate";

/** Stable per-row key. Ids are user-editable, so never key on them. */
export function rowKey(entry: ImportEntry): string {
  return `${entry.scope}:${entry.project_path ?? ""}:${entry.original_id}`;
}

/**
 * Group entries by (scope, project_path) — NOT by the display label, so two
 * distinct projects that share a basename (both "web") stay separate groups.
 * First-seen order is preserved (user scope first, then projects in file
 * order, matching the backend).
 */
export function groupEntries(entries: ImportEntry[]): ImportGroup[] {
  const groups: ImportGroup[] = [];
  const byKey = new Map<string, ImportGroup>();
  for (const e of entries) {
    const key = `${e.scope}:${e.project_path ?? ""}`;
    let g = byKey.get(key);
    if (!g) {
      g = { label: e.group_label, scope: e.scope, projectPath: e.project_path, entries: [] };
      byKey.set(key, g);
      groups.push(g);
    }
    g.entries.push(e);
  }
  return groups;
}

/** Default selection: everything addable except blocked and duplicate rows. */
export function defaultSelectedKeys(entries: ImportEntry[]): Set<string> {
  const selected = new Set<string>();
  for (const e of entries) {
    if (!e.blocked && !e.duplicate_of) selected.add(rowKey(e));
  }
  return selected;
}

/** rowKey -> default (proposed) id. */
export function defaultIds(entries: ImportEntry[]): Map<string, string> {
  const ids = new Map<string, string>();
  for (const e of entries) ids.set(rowKey(e), e.proposed_id);
  return ids;
}

/**
 * Validate the ids of the *selected* rows. A row is invalid when its id is
 * empty, already used by an existing upstream, or duplicated by another
 * selected row. Returns only the offending rows (rowKey -> reason).
 */
export function computeIdErrors(
  selectedKeys: Set<string>,
  ids: Map<string, string>,
  existingIds: Set<string>,
): Map<string, IdErrorCode> {
  const counts = new Map<string, number>();
  for (const key of selectedKeys) {
    const id = (ids.get(key) ?? "").trim();
    counts.set(id, (counts.get(id) ?? 0) + 1);
  }
  const errors = new Map<string, IdErrorCode>();
  for (const key of selectedKeys) {
    const id = (ids.get(key) ?? "").trim();
    if (!id) {
      errors.set(key, "empty");
    } else if (existingIds.has(id)) {
      errors.set(key, "exists");
    } else if ((counts.get(id) ?? 0) > 1) {
      errors.set(key, "duplicate");
    }
  }
  return errors;
}

/** Return `value` if it's an array, else `[]` — guards against a backend
 *  that sends a missing / null / wrong-typed list field. */
function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * Coerce a preview response so its list fields are ALWAYS real arrays.
 * The dialog calls `res.entries.length`, `defaultSelectedKeys(res.entries)`,
 * `res.parse_errors.map(...)`, etc.; a missing/null/malformed field would
 * otherwise throw "reading 'length'" / "not iterable" the moment the
 * /import/preview response lands (the class of bug behind the prod crash).
 */
export function normalizePreviewResponse(
  res: Partial<ImportPreviewResponse> | null | undefined,
): ImportPreviewResponse {
  return {
    entries: asArray<ImportEntry>(res?.entries),
    parse_errors: asArray<string>(res?.parse_errors),
    existing_ids: asArray<string>(res?.existing_ids),
  };
}

/** Same array guard for the confirm result (added / skipped / errors). */
export function normalizeResultResponse(
  res: Partial<ImportResultResponse> | null | undefined,
): ImportResultResponse {
  return {
    added: asArray<string>(res?.added),
    skipped: asArray<string>(res?.skipped),
    errors: asArray<ImportErrorDetail>(res?.errors),
  };
}

/** Build the confirm payload from the selected rows + their current ids. */
export function buildConfirmEntries(
  entries: ImportEntry[],
  selectedKeys: Set<string>,
  ids: Map<string, string>,
): ImportConfirmEntry[] {
  const result: ImportConfirmEntry[] = [];
  for (const e of entries) {
    const key = rowKey(e);
    if (!selectedKeys.has(key)) continue;
    result.push({
      scope: e.scope,
      project_path: e.project_path,
      original_id: e.original_id,
      target_id: (ids.get(key) ?? e.proposed_id).trim(),
    });
  }
  return result;
}
