/**
 * Per-MCP template variables UI — list, add, replace, rename, delete.
 *
 * Three operating modes via prop:
 *   - **buffered**: ``upstreamId=""``. Mutations live in local
 *     component state and surface via ``onBufferedChange`` so the
 *     create wizard can submit them atomically with the create call.
 *   - **deferred**: ``upstreamId!=""`` plus ``pendingChanges`` /
 *     ``onPendingChange``. Mutations land in a parent-owned buffer
 *     and only commit when the SETTINGS Edit/Save click flushes
 *     them via ``PUT /api/admin/upstreams/{id}`` with
 *     ``template_var_changes``. Cancel discards the buffer.
 *   - **read-only**: ``readOnly=true``. Server list renders without
 *     any Add/Replace/Delete affordances; used in view mode of the
 *     upstream detail page.
 *
 * Two flavours of variable coexist in the same list, distinguished
 * by ``is_secret``:
 *   - **Password** (``is_secret=true``, the default at create time):
 *     value is obfuscated by default in the UI as ``••••XYZ4`` with
 *     an eye toggle to reveal it (1Password-style). The plaintext
 *     IS returned by the API — encryption-at-rest in cloud mode is
 *     the security boundary, not "never echo back".
 *   - **Plain** (``is_secret=false``): value rendered verbatim,
 *     useful for non-sensitive config like feature flags.
 *
 * The UI-facing "Treat as password" toggle (the ``is_secret`` flag
 * in the backend) is rendered only on **Add**; on **Replace** the
 * existing record's flag is preserved (the backend enforces this;
 * the UI just hides the toggle to make it obvious).
 *
 * **Renames**: in Replace mode the name field is editable. If the
 * user changes the name, the buffer treats the save as
 * delete-old + set-new, atomically on the SETTINGS Save click.
 */
import type { JSX } from "react";
import { useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { AlertTriangle, Check, Copy, Eye, EyeOff, Pencil, Plus, Trash2, X } from "lucide-react";

import { listTemplateVars as apiListTemplateVars } from "../api/template-vars";
import { listSystemVariables as apiListSystemVariables } from "../api/sandbox-files";
import type {
  SystemVariable,
  TemplateVarSummary,
} from "../api/types";
import { ConfirmDialog, useConfirm } from "./ConfirmDialog";
import { FieldHint } from "./ui/field-hint";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import {
  analyseReferences,
  type ReferenceAnalysis,
  type TemplateVarReference,
} from "../lib/template-var-references";

const NAME_RE = /^[A-Z_][A-Z0-9_]*$/;
const PLAIN_VALUE_TRUNCATE_AT = 40;

export interface BufferedTemplateVar {
  value: string;
  is_secret: boolean;
}

/** Deferred-mode buffer of pending env-var mutations.
 *
 * The Edit/Save flow accumulates Add/Replace/Delete clicks in this
 * shape and ships them to the backend in one ``template_var_changes``
 * payload on SETTINGS Save. Cancel discards.
 *
 * Invariants kept by the manager:
 *   - A name in ``deletes`` is never also in ``sets`` (an Add after
 *     a Delete moves the name from ``deletes`` back into ``sets``).
 *   - A buffered-only Add followed by Delete in the same session
 *     drops the name from ``sets`` without touching ``deletes`` —
 *     server has nothing to delete.
 */
export interface PendingTemplateVarChanges {
  sets: Record<string, BufferedTemplateVar>;
  deletes: string[];
}

export const EMPTY_PENDING_CHANGES: PendingTemplateVarChanges = { sets: {}, deletes: [] };

export interface TemplateVarsManagerHandle {
  /** Open the Add modal pre-filled with ``name`` (locked). */
  openDefine: (name: string) => void;
}

interface TemplateVarsManagerProps {
  /** Empty for buffered (create wizard). Non-empty drives the
   *  server-list fetch; combine with ``pendingChanges`` /
   *  ``onPendingChange`` for the deferred edit-mode buffer. */
  upstreamId: string;
  /** References found in the current JSON config (drives badges +
   *  unresolved warnings). */
  references: TemplateVarReference[];
  /** Buffered-mode initial values (defaults to ``{}``). */
  initialBuffered?: Record<string, BufferedTemplateVar>;
  /** Buffered-mode change callback — receives the full
   *  ``{name: {value, is_secret}}`` map every time the user adds,
   *  replaces, or deletes. */
  onBufferedChange?: (values: Record<string, BufferedTemplateVar>) => void;
  /** Deferred-mode pending mutations (parent-owned). Presence of
   *  this prop puts the manager in deferred mode: mutations write
   *  to ``onPendingChange`` instead of hitting the API. The parent
   *  flushes the buffer on SETTINGS Save and clears it on Cancel. */
  pendingChanges?: PendingTemplateVarChanges;
  /** Deferred-mode mutation sink. Required when ``pendingChanges``
   *  is set. */
  onPendingChange?: (next: PendingTemplateVarChanges) => void;
  /** Optional ref handle for the JSON editor's "click unresolved
   *  token" affordance. */
  handleRef?: React.RefObject<TemplateVarsManagerHandle | null>;
  /** When ``true``, hides every Add / Replace / Delete affordance.
   *  Used on the upstream detail page in view mode so an admin can
   *  see what's defined without giving them surprise live-API
   *  buttons next to a section that otherwise reads as static
   *  metadata. The list itself, the password mask / verbatim
   *  display, the per-row pill, and the Referenced / Unreferenced
   *  badges all still render. */
  readOnly?: boolean;
  /** Stdio upstreams also expand placeholders into Sandbox file
   *  ``target_path`` strings, so the header subtitle mentions the
   *  file paths and the "not OS env vars" caveat. HTTP upstreams
   *  only see expansion in the JSON config. Defaults to ``false``. */
  isStdio?: boolean;
}

function computeLastFourClient(value: string): string | null {
  return value.length > 16 ? value.slice(-4) : null;
}

function maskedDisplay(lastFour: string | null): string {
  return lastFour ? `••••${lastFour}` : "••••";
}

function summaryFromBuffered(
  name: string,
  spec: BufferedTemplateVar,
): TemplateVarSummary {
  const isoZero = new Date(0).toISOString();
  return {
    name,
    is_secret: spec.is_secret,
    // The list path now carries plaintext for password rows too —
    // the SPA obfuscates them by default with an eye toggle.
    value: spec.value,
    last_four: spec.is_secret ? computeLastFourClient(spec.value) : null,
    created_at: isoZero,
    updated_at: isoZero,
  };
}

/** Overlay deferred-mode pending mutations on top of the server list.
 *
 * Server rows queued for delete drop out. Server rows in ``sets``
 * keep their ``is_secret`` (the repository preserves the flag on
 * replace) but get the new value rendered optimistically. Names
 * only in ``sets`` (no server row) render as fresh additions.
 */
function mergePendingWithServer(
  server: TemplateVarSummary[],
  pending: PendingTemplateVarChanges,
): TemplateVarSummary[] {
  const deleteSet = new Set(pending.deletes);
  const result: TemplateVarSummary[] = [];
  const serverNames = new Set<string>();
  for (const s of server) {
    serverNames.add(s.name);
    if (deleteSet.has(s.name)) continue;
    const override = pending.sets[s.name];
    if (override) {
      // Replace: server's is_secret wins (repository contract).
      result.push({
        ...s,
        value: override.value,
        last_four: s.is_secret
          ? computeLastFourClient(override.value)
          : null,
        updated_at: new Date().toISOString(),
      });
    } else {
      result.push(s);
    }
  }
  for (const [name, spec] of Object.entries(pending.sets)) {
    if (serverNames.has(name)) continue;
    result.push(summaryFromBuffered(name, spec));
  }
  result.sort((a, b) => a.name.localeCompare(b.name));
  return result;
}

export function TemplateVarsManager({
  upstreamId,
  references,
  initialBuffered,
  onBufferedChange,
  pendingChanges,
  onPendingChange,
  handleRef,
  readOnly = false,
  isStdio = false,
}: TemplateVarsManagerProps): JSX.Element {
  const isBuffered = upstreamId === "";
  const isDeferred = !isBuffered && pendingChanges !== undefined;

  // Buffered-mode local store (kept aligned with ``envVars`` by name).
  const bufferRef = useRef<Map<string, BufferedTemplateVar>>(
    new Map(Object.entries(initialBuffered ?? {})),
  );
  // Server list — populated for non-buffered modes; empty for buffered.
  const [serverList, setServerList] = useState<TemplateVarSummary[]>([]);
  const [bufferedList, setBufferedList] = useState<TemplateVarSummary[]>(() => {
    if (!isBuffered) return [];
    return Object.entries(initialBuffered ?? {}).map(
      ([name, spec]) => summaryFromBuffered(name, spec),
    );
  });
  const [loading, setLoading] = useState(!isBuffered);
  const [error, setError] = useState<string | null>(null);

  // System Variables (``${HOME}``, …) — read-only, surfaced at the
  // top of the list with a "system" badge so operators can discover
  // and reference them. Sourced from the live sandbox env at launch
  // by the backend; fetched here to render the resolved value
  // eagerly without a launch round-trip.
  const [systemVars, setSystemVars] = useState<SystemVariable[]>([]);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const transport = isStdio ? "stdio" : "streamable_http";
      if (isBuffered) {
        // No upstream id yet — only system Variables can be fetched.
        // User Variables live in the local buffer until the wizard
        // submits the create call. Don't toggle ``loading`` here:
        // the operator is already looking at the local buffer; a
        // background fetch for the read-only system row shouldn't
        // collapse the list to "Loading…".
        const sysVars = await apiListSystemVariables(transport);
        setSystemVars(sysVars);
      } else {
        setLoading(true);
        try {
          const [list, sysVars] = await Promise.all([
            apiListTemplateVars(upstreamId),
            apiListSystemVariables(transport),
          ]);
          setServerList(list);
          setSystemVars(sysVars);
        } finally {
          setLoading(false);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [isBuffered, isStdio, upstreamId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The list rendered to the user. Buffered: bufferedList. Deferred:
  // server overlaid with pending. Read-only / fallback: server.
  const envVars: TemplateVarSummary[] = isBuffered
    ? bufferedList
    : isDeferred && pendingChanges
      ? mergePendingWithServer(serverList, pendingChanges)
      : serverList;

  const existingNames = new Set(envVars.map((s) => s.name));

  // Modal state.
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<"add" | "replace">("add");
  const [modalName, setModalName] = useState("");
  // ``modalNameLocked`` is now used only for the "Define <NAME>"
  // affordance (openDefine pre-fills + locks the name so the user
  // commits the variable they came from). In Replace mode the name
  // is editable (rename support); ``modalOriginalName`` tracks the
  // pre-edit value so the buffer can do delete-old + set-new.
  const [modalNameLocked, setModalNameLocked] = useState(false);
  const [modalOriginalName, setModalOriginalName] = useState("");
  const [modalValue, setModalValue] = useState("");
  const [modalIsSecret, setModalIsSecret] = useState(true);
  const [modalReveal, setModalReveal] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  // The is_secret of the row being replaced — drives the input
  // masking + which copy renders below.
  const [modalReplacingSecret, setModalReplacingSecret] = useState(true);

  const openAdd = useCallback(() => {
    setModalMode("add");
    setModalName("");
    setModalNameLocked(false);
    setModalOriginalName("");
    setModalValue("");
    setModalIsSecret(true);
    setModalReveal(false);
    setModalError(null);
    setModalReplacingSecret(true);
    setModalOpen(true);
  }, []);

  const openReplace = useCallback(
    (name: string, isSecret: boolean, currentValue: string | null) => {
      setModalMode("replace");
      setModalName(name);
      setModalOriginalName(name);
      // Name is editable on Replace: users can rename a variable
      // without delete + re-add. The buffer logic in handleSave
      // handles delete-old + set-new when the name changes.
      setModalNameLocked(false);
      // Prefill the value from the list so editing feels like a
      // normal text field. Both kinds carry the plaintext now — the
      // SPA obfuscates password rows by default and exposes an eye
      // toggle to reveal.
      setModalValue(currentValue ?? "");
      setModalIsSecret(isSecret);
      setModalReplacingSecret(isSecret);
      // Plain rows show the value while typing; password rows start
      // obfuscated (eye toggle reveals).
      setModalReveal(!isSecret);
      setModalError(null);
      setModalOpen(true);
    },
    [],
  );

  const openDefine = useCallback((name: string) => {
    setModalMode("add");
    setModalName(name);
    // openDefine targets a specific unresolved reference; lock the
    // name so the user commits the variable they came from. (Add
    // and Replace both leave it editable.)
    setModalNameLocked(true);
    setModalOriginalName("");
    setModalValue("");
    setModalIsSecret(true);
    setModalReveal(false);
    setModalError(null);
    setModalReplacingSecret(true);
    setModalOpen(true);
  }, []);

  useImperativeHandle(handleRef, () => ({ openDefine }), [openDefine, handleRef]);
  // Keep the ref in sync (for the rare case the parent assigns it
  // before the imperative handle effect runs).
  useEffect(() => {
    if (handleRef) handleRef.current = { openDefine };
    return () => {
      if (handleRef && handleRef.current) handleRef.current = null;
    };
  }, [handleRef, openDefine]);

  const closeModal = useCallback(() => {
    setModalOpen(false);
    setModalValue(""); // never leave plaintext sitting in component state
  }, []);

  const handleSave = useCallback(() => {
    if (!NAME_RE.test(modalName)) {
      setModalError("Name must match [A-Z_][A-Z0-9_]*");
      return;
    }
    // Empty values are allowed: ``${EMPTY}`` substitution becomes the
    // empty string, which is sometimes the intended value (passing
    // ``--flag ""`` through ``args``, clearing a header, etc.).
    // Rename collision guard: if the user typed a new name on
    // Replace, the new name must not already be in use.
    const isRename =
      modalMode === "replace" && modalName !== modalOriginalName;
    if (
      modalMode === "add" && existingNames.has(modalName)
    ) {
      setModalError(
        `A variable named "${modalName}" already exists. Replace it ` +
        "from the list instead, or pick a different name.",
      );
      return;
    }
    if (isRename && existingNames.has(modalName)) {
      setModalError(
        `A variable named "${modalName}" already exists. Pick a ` +
        "different name.",
      );
      return;
    }
    setModalError(null);
    // On Replace, the row's existing is_secret wins (backend enforces;
    // we mirror so the buffered/deferred paths agree with the bound path).
    const effectiveIsSecret =
      modalMode === "replace" ? modalReplacingSecret : modalIsSecret;
    const spec: BufferedTemplateVar = {
      value: modalValue,
      is_secret: effectiveIsSecret,
    };
    if (isBuffered) {
      // Rename support: drop the old key first when the user edits
      // the name on Replace. Same dict shape post-write either way.
      if (isRename) {
        bufferRef.current.delete(modalOriginalName);
      }
      bufferRef.current.set(modalName, spec);
      const summary = summaryFromBuffered(modalName, spec);
      setBufferedList((prev) => {
        const filteredOldName = isRename
          ? prev.filter((s) => s.name !== modalOriginalName)
          : prev;
        const filtered = filteredOldName.filter((s) => s.name !== modalName);
        const created =
          prev.find((s) => s.name === modalName)?.created_at
          ?? prev.find((s) => s.name === modalOriginalName)?.created_at
          ?? summary.created_at;
        return [
          ...filtered,
          { ...summary, created_at: created },
        ].sort((a, b) => a.name.localeCompare(b.name));
      });
      onBufferedChange?.(Object.fromEntries(bufferRef.current));
    } else if (isDeferred && pendingChanges && onPendingChange) {
      // Deferred: write to parent-owned buffer. Three sub-cases:
      //
      // 1. Replace without rename — ``sets[modalName] = spec``;
      //    drop modalName from deletes (un-delete on re-add).
      // 2. Add (or unrelated to rename) — same shape as (1).
      // 3. Rename — drop the OLD name from sets; if the OLD name is
      //    on the server (i.e. has a server-side row to remove on
      //    flush), add it to deletes; then set the NEW name. The
      //    repository contract preserves is_secret on the original
      //    row, but a renamed variable is a brand-new record on the
      //    server, so we send the spec's flag through.
      const oldName = isRename ? modalOriginalName : null;
      const oldOnServer =
        oldName !== null && serverList.some((s) => s.name === oldName);

      const nextSets = { ...pendingChanges.sets };
      if (oldName !== null) delete nextSets[oldName];
      nextSets[modalName] = spec;

      let nextDeletes = pendingChanges.deletes.filter(
        (n) => n !== modalName,
      );
      if (oldName !== null && oldOnServer && !nextDeletes.includes(oldName)) {
        nextDeletes = [...nextDeletes, oldName];
      }
      onPendingChange({ sets: nextSets, deletes: nextDeletes });
    }
    closeModal();
  }, [
    closeModal, existingNames, isBuffered, isDeferred,
    modalIsSecret, modalMode, modalName, modalOriginalName,
    modalReplacingSecret, modalValue, onBufferedChange,
    onPendingChange, pendingChanges, serverList,
  ]);

  // Reference analysis drives three surfaces inside this card:
  //   - per-row Referenced N× / Unreferenced badge (from byName)
  //   - the "undefined variables" callout above the list, listing
  //     each unresolved name once with an inline Add button
  //   - the Delete-confirmation copy (warns only when referenced)
  // ``defined`` includes both user Variables and system Variables —
  // a reference to ``${HOME}`` from anywhere (JSON config or a
  // Sandbox-file ``target_path``) must NOT show up in the amber
  // "undefined" callout. The runtime resolver treats system vars
  // as a first layer; the analyser mirrors that.
  const analysis: ReferenceAnalysis = analyseReferences(
    references,
    [...envVars.map((s) => s.name), ...systemVars.map((sv) => sv.name)],
  );
  const unresolvedNames: string[] = Array.from(
    new Set(analysis.unresolved.map((r) => r.name)),
  ).sort();

  // Delete confirmation.
  const { confirm, dialogProps } = useConfirm();

  const handleDelete = useCallback(
    async (name: string) => {
      // Reference count drives whether to confirm at all: deleting
      // an unreferenced variable is harmless (no running config
      // depends on it), so we skip the prompt and act immediately.
      // A referenced variable would break the next session, so the
      // confirm dialog spells that out.
      const refCount = analysis.byName.get(name)?.count ?? 0;
      if (refCount > 0) {
        const ok = await confirm({
          title: `Delete ${name}?`,
          message:
            `\${${name}} is referenced in this MCP's config. Deleting ` +
            "it will cause the next session to fail until you set it " +
            "again or remove the references.",
          confirmLabel: "Delete",
          cancelLabel: "Cancel",
          destructive: true,
        });
        if (!ok) return;
      }
      try {
        if (isBuffered) {
          bufferRef.current.delete(name);
          setBufferedList((prev) => prev.filter((s) => s.name !== name));
          onBufferedChange?.(Object.fromEntries(bufferRef.current));
        } else if (isDeferred && pendingChanges && onPendingChange) {
          const onServer = serverList.some((s) => s.name === name);
          const nextSets = { ...pendingChanges.sets };
          delete nextSets[name];
          const nextDeletes =
            onServer && !pendingChanges.deletes.includes(name)
              ? [...pendingChanges.deletes, name]
              : pendingChanges.deletes;
          onPendingChange({ sets: nextSets, deletes: nextDeletes });
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [
      analysis, confirm, isBuffered, isDeferred, onBufferedChange,
      onPendingChange, pendingChanges, serverList,
    ],
  );

  return (
    <div className="space-y-3">
      {/* Title + Add affordance on a single row. The container
          (``SettingsCard`` on the detail page + create wizard, or a
          plain wrapper in tests) does NOT provide a section label
          for the Variables block — the manager owns its title so
          the Add button can sit inline next to it. */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Variables
          </span>
          <p className="mt-0.5 text-xs text-zinc-500">
            {isStdio
              ? <>Placeholders expanded in the JSON config file and target file paths. Not OS environment variables. To pass environment variables to the sandbox subprocess, consider adding them to the JSON config under the <code>"env"</code> property.</>
              : "Placeholders expanded into the config JSON."}
          </p>
        </div>
        {!readOnly && (
          <button
            type="button"
            onClick={openAdd}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded shrink-0"
          >
            <Plus size={12} /> Add variable
          </button>
        )}
      </div>

      {error && (
        <p className="text-sm text-red-600">{error}</p>
      )}

      {!readOnly && unresolvedNames.length > 0 && (
        <div className="border border-amber-200 rounded p-2 bg-amber-50 text-xs">
          <div className="flex items-center gap-1.5 text-amber-800 mb-1.5">
            <AlertTriangle size={12} />
            <span>This config references undefined variables</span>
          </div>
          <ul className="space-y-1">
            {unresolvedNames.map((name) => (
              <li key={name} className="flex items-center gap-2">
                <code className="font-mono text-amber-900">
                  {`\${${name}}`}
                </code>
                <button
                  type="button"
                  onClick={() => openDefine(name)}
                  className="px-1.5 py-0.5 text-[10px] bg-amber-600 hover:bg-amber-700 text-white rounded inline-flex items-center gap-1"
                >
                  <Plus size={10} /> Add
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {loading ? (
        <p className="text-xs text-zinc-500">Loading…</p>
      ) : envVars.length === 0 && systemVars.length === 0 ? (
        <p className="text-xs text-zinc-500 italic">
          No variables defined.
        </p>
      ) : (
        <ul className="divide-y divide-zinc-200 border border-zinc-200 rounded">
          {systemVars.map((sv) => {
            const refCount =
              analysis.byName.get(sv.name)?.count ?? 0;
            return (
              <li
                key={`system-${sv.name}`}
                className="flex items-center justify-between px-3 py-2 text-sm gap-3 bg-zinc-50"
              >
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <span className="font-mono text-xs text-zinc-900 shrink-0">
                    {sv.name}
                  </span>
                  <span className="font-mono text-xs text-zinc-700 truncate">
                    {sv.value}
                  </span>
                  <span
                    className="px-1.5 py-0.5 text-[10px] bg-blue-100 text-blue-800 rounded shrink-0"
                    title="Read-only system variable injected at sandbox launch"
                  >
                    system
                  </span>
                  {refCount > 0 && (
                    <span className="px-1.5 py-0.5 text-[10px] bg-emerald-100 text-emerald-800 rounded shrink-0">
                      Referenced {refCount}×
                    </span>
                  )}
                </div>
              </li>
            );
          })}
          {envVars.map((envVar) => {
            const info = analysis.byName.get(envVar.name);
            const refCount = info?.count ?? 0;
            // Resolved-preview hint: when a Variable's value contains
            // any system Variable reference (``${HOME}`` etc.), show
            // the resolved string under the value so the operator
            // sees what the launcher will substitute.
            const containsSystemRef =
              envVar.value !== null &&
              envVar.value.includes("${") &&
              systemVars.some((sv) =>
                envVar.value!.includes(`\${${sv.name}}`),
              );
            return (
              <li
                key={envVar.name}
                className="flex flex-col gap-1 px-3 py-2 text-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3 min-w-0 flex-1">
                  <span className="font-mono text-xs text-zinc-900 shrink-0">
                    {envVar.name}
                  </span>
                  {envVar.value !== null ? (
                    envVar.is_secret ? (
                      <PasswordValueCell
                        value={envVar.value}
                        lastFour={envVar.last_four}
                      />
                    ) : (
                      <PlainValueCell value={envVar.value} />
                    )
                  ) : (
                    <span className="font-mono text-xs text-zinc-400 italic">
                      (no value)
                    </span>
                  )}
                  {envVar.is_secret && (
                    <span className="px-1.5 py-0.5 text-[10px] bg-zinc-200 text-zinc-700 rounded shrink-0">
                      password
                    </span>
                  )}
                  {refCount > 0 ? (
                    <span className="px-1.5 py-0.5 text-[10px] bg-emerald-100 text-emerald-800 rounded shrink-0">
                      Referenced {refCount}×
                    </span>
                  ) : (
                    <span className="px-1.5 py-0.5 text-[10px] bg-zinc-100 text-zinc-600 rounded shrink-0">
                      Unreferenced
                    </span>
                  )}
                </div>
                {!readOnly && (
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      type="button"
                      onClick={() => openReplace(envVar.name, envVar.is_secret, envVar.value)}
                      className="p-1 text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100 rounded"
                      title="Replace value"
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(envVar.name)}
                      className="p-1 text-zinc-500 hover:text-red-700 hover:bg-red-50 rounded"
                      title="Delete variable"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )}
                </div>
                {containsSystemRef && envVar.value && (
                  <div className="text-[11px] text-zinc-500 ml-1 italic">
                    Resolved at launch:{" "}
                    <code className="font-mono">
                      {systemVars.reduce(
                        (s, sv) => s.split(`\${${sv.name}}`).join(sv.value),
                        envVar.value,
                      )}
                    </code>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="fixed inset-0 bg-black/40" onClick={closeModal} />
          <div role="dialog" className="relative bg-white rounded-lg shadow-lg p-6 max-w-sm w-full mx-4 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-base font-semibold text-zinc-900">
                {modalMode === "add"
                  ? "Add variable"
                  : `Edit ${modalOriginalName} (${modalReplacingSecret ? "password" : "plain"})`}
              </h3>
              <button
                onClick={closeModal}
                className="text-zinc-400 hover:text-zinc-600"
              >
                <X size={16} />
              </button>
            </div>
            <div>
              <label
                htmlFor="env-var-modal-name"
                className="text-xs font-medium text-zinc-700"
              >
                Name
              </label>
              <input
                id="env-var-modal-name"
                value={modalName}
                onChange={(e) => setModalName(e.target.value.toUpperCase())}
                disabled={modalNameLocked}
                placeholder="GITHUB_TOKEN"
                className="mt-1 w-full px-2 py-1.5 border border-zinc-300 rounded text-sm font-mono disabled:bg-zinc-100"
                autoFocus={!modalNameLocked}
              />
              <FieldHint>
                Uppercase letters, digits, underscore. Must start with a
                letter or underscore.
              </FieldHint>
            </div>
            {modalMode === "add" && (
              <div>
                <label className="flex items-center gap-2 text-xs text-zinc-700">
                  <input
                    type="checkbox"
                    checked={modalIsSecret}
                    onChange={(e) => setModalIsSecret(e.target.checked)}
                  />
                  <span className="font-medium">Treat as password</span>
                </label>
                <FieldHint>
                  When on: value is obfuscated by default in the list;
                  click the eye icon on the row to reveal it.
                </FieldHint>
              </div>
            )}
            <div>
              <label
                htmlFor="env-var-modal-value"
                className="text-xs font-medium text-zinc-700"
              >
                Value
              </label>
              <div className="relative mt-1">
                <input
                  id="env-var-modal-value"
                  type={
                    (modalMode === "add" ? modalIsSecret : modalReplacingSecret) && !modalReveal
                      ? "password"
                      : "text"
                  }
                  value={modalValue}
                  onChange={(e) => setModalValue(e.target.value)}
                  placeholder="Paste value"
                  className="w-full px-2 py-1.5 pr-9 border border-zinc-300 rounded text-sm font-mono"
                  autoFocus={modalNameLocked}
                />
                {(modalMode === "add" ? modalIsSecret : modalReplacingSecret) && (
                  <button
                    type="button"
                    onClick={() => setModalReveal((r) => !r)}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 text-zinc-400 hover:text-zinc-700"
                    title={modalReveal ? "Hide" : "Show while typing"}
                  >
                    {modalReveal ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                )}
              </div>
            </div>
            {modalError && (
              <p className="text-sm text-red-600">{modalError}</p>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={closeModal}
                className="px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 rounded"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={!NAME_RE.test(modalName)}
                className="px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog {...dialogProps} />
    </div>
  );
}

/** Plain (non-secret) row body: truncated value with a hover popover
 *  for the full string and a one-click copy button.
 *
 *  - Short values (≤ ``PLAIN_VALUE_TRUNCATE_AT``) render verbatim
 *    with no popover (the trigger is still rendered as a span so
 *    layout stays consistent).
 *  - Long values render the truncated display; hover surfaces the
 *    full value in a tooltip (replaces the v1 ``title=`` browser
 *    tooltip so it is keyboard-accessible and styleable).
 *  - The copy button copies the full plaintext and briefly flips to
 *    a ✓ for visual confirmation.
 */
function PlainValueCell({ value }: { value: string }): JSX.Element {
  const truncated = value.length > PLAIN_VALUE_TRUNCATE_AT;
  const display = truncated
    ? value.slice(0, PLAIN_VALUE_TRUNCATE_AT) + "…"
    : value;
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard write can fail in non-secure contexts or when the
      // permission is denied; swallow so the UI doesn't error out.
    }
  }, [value]);
  const text = (
    <span className="font-mono text-xs text-zinc-700 truncate">
      {display}
    </span>
  );
  return (
    <span className="flex items-center gap-1 min-w-0">
      {truncated ? (
        <Tooltip>
          <TooltipTrigger render={(props) => <span {...props}>{text}</span>} />
          <TooltipContent className="font-mono">{value}</TooltipContent>
        </Tooltip>
      ) : (
        text
      )}
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="p-0.5 text-zinc-400 hover:text-zinc-700 shrink-0"
        title={copied ? "Copied" : "Copy value"}
        aria-label={copied ? "Copied" : "Copy value"}
      >
        {copied ? <Check size={11} /> : <Copy size={11} />}
      </button>
    </span>
  );
}

/** Password row body: 1Password-style obfuscated-by-default display
 *  with an eye toggle to reveal the value, plus a one-click copy
 *  button that always copies the plaintext (no reveal needed).
 *
 *  - Default: ``••••XYZ4`` (or ``••••`` if the value is too short for
 *    a last-4 preview).
 *  - Revealed: full value rendered the same way ``PlainValueCell``
 *    does — truncated with a hover tooltip when long.
 *  - Copy: same affordance as the plain cell. Distinct from "reveal"
 *    so the operator can paste a secret without putting it on screen.
 */
function PasswordValueCell({
  value, lastFour,
}: { value: string; lastFour: string | null }): JSX.Element {
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard write can fail in non-secure contexts; ignore so
      // the UI doesn't error out.
    }
  }, [value]);
  const truncated = value.length > PLAIN_VALUE_TRUNCATE_AT;
  const revealedDisplay = truncated
    ? value.slice(0, PLAIN_VALUE_TRUNCATE_AT) + "…"
    : value;
  return (
    <span className="flex items-center gap-1 min-w-0">
      {revealed ? (
        truncated ? (
          <Tooltip>
            <TooltipTrigger
              render={(props) => (
                <span {...props}>
                  <span className="font-mono text-xs text-zinc-700 truncate">
                    {revealedDisplay}
                  </span>
                </span>
              )}
            />
            <TooltipContent className="font-mono">{value}</TooltipContent>
          </Tooltip>
        ) : (
          <span className="font-mono text-xs text-zinc-700 truncate">
            {revealedDisplay}
          </span>
        )
      ) : (
        <span className="font-mono text-xs text-zinc-500 shrink-0">
          {maskedDisplay(lastFour)}
        </span>
      )}
      <button
        type="button"
        onClick={() => setRevealed((r) => !r)}
        className="p-0.5 text-zinc-400 hover:text-zinc-700 shrink-0"
        title={revealed ? "Hide value" : "Reveal value"}
        aria-label={revealed ? "Hide value" : "Reveal value"}
      >
        {revealed ? <EyeOff size={11} /> : <Eye size={11} />}
      </button>
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="p-0.5 text-zinc-400 hover:text-zinc-700 shrink-0"
        title={copied ? "Copied" : "Copy value"}
        aria-label={copied ? "Copied" : "Copy value"}
      >
        {copied ? <Check size={11} /> : <Copy size={11} />}
      </button>
    </span>
  );
}
