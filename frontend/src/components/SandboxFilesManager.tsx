/**
 * Per-MCP **Sandbox files** UI — list, add (with drag-drop upload),
 * replace, delete.
 *
 * Sibling of ``TemplateVarsManager``. Files do NOT participate in
 * ``${...}`` substitution — they're materialised at the path the
 * operator picks, full stop. The canonical "file on disk + env var
 * pointing at it" recipe is wired by typing the same ``${HOME}/...``
 * literal twice (once in the file's ``target_path``, once in the
 * Variable's value); both render to the same absolute path at
 * launch.
 *
 * ``target_path`` accepts the same ``${...}`` references as env-var
 * values (system + user Variables), so an operator can parameterize
 * paths per-tenant / per-profile without re-uploading the file.
 * Unknown tokens at launch surface as ``MissingTemplateVarError``;
 * undefined references show up in the Variables card's amber
 * "undefined variables" callout because the detail page scans
 * target_paths the same way it scans the JSON config.
 *
 * The list shows name + resolved target path + size + truncated
 * sha256 + last-modified. Contents are NEVER displayed verbatim
 * (size + sha256 only) — too easy to leak by accident on the
 * detail page.
 *
 * v1 server-direct only: no buffered / deferred mode. Add / Replace
 * / Delete hit the API immediately. The detail page's SETTINGS Save
 * flow does not need to flush a Files buffer because the files have
 * already been written. The runtime hash includes file metadata so
 * a Files mutation triggers the dirty banner the same way a
 * Variable edit does.
 */
import type { JSX } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FileText, Plus, Trash2, Upload } from "lucide-react";

import {
  deleteSandboxFile,
  listSandboxFiles,
  listSystemVariables,
  setSandboxFile,
} from "../api/sandbox-files";
import { listTemplateVars } from "../api/template-vars";
import type { SandboxFileSummary, SystemVariable, TemplateVarSummary } from "../api/types";
import { slugifySandboxFileName } from "../lib/sandbox-file-slug";
import {
  analyseReferences,
  findReferencesInSandboxFiles,
} from "../lib/template-var-references";
import { ConfirmDialog, useConfirm } from "./ConfirmDialog";

// Mirror of ``MAX_FILE_BYTES`` in
// ``backend/src/mcpolis/domain/model/sandbox_file.py``. Sized for
// credential / config files (~5x headroom over a pathological CA
// bundle); an accidental binary upload errors immediately.
const MAX_FILE_BYTES = 128 * 1024;
const SHA_DISPLAY_CHARS = 12;

/** Pick a fresh URL-safe id for a new file. Slugifies the display
 *  name; falls back to ``file-<6-char random>`` when the display
 *  name has no usable characters. Appends ``-2``, ``-3``, … on
 *  collision with an existing file's name. */
function makeUniqueFileId(
  displayName: string,
  existingNames: ReadonlyArray<string>,
): string {
  const base =
    slugifySandboxFileName(displayName)
    || `file-${Math.random().toString(36).slice(2, 8)}`;
  if (!existingNames.includes(base)) return base;
  for (let i = 2; i < 100; i += 1) {
    const candidate = `${base}-${i}`;
    if (!existingNames.includes(candidate)) return candidate;
  }
  // Fallback for the unbelievable-case operator with 100 collisions.
  return `${base}-${Math.random().toString(36).slice(2, 8)}`;
}

interface SandboxFilesManagerProps {
  upstreamId: string;
  /** When true, hides every Add / Replace / Delete affordance.
   *  Used in view mode of the upstream detail page so an admin can
   *  see what's defined without surprise live-API buttons. */
  readOnly?: boolean;
  /** Fires after every successful Add / Replace / Delete so the
   *  parent (the upstream detail page) can re-fetch its own copy of
   *  the file list — used by the Variables card to scan target_paths
   *  for ``${...}`` references. Files are server-direct (no buffer),
   *  so a parent that wants the change picked up before a Save
   *  reload needs this hook. */
  onFilesChanged?: () => void;
}

function bytesToHuman(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export function SandboxFilesManager({
  upstreamId,
  readOnly = false,
  onFilesChanged,
}: SandboxFilesManagerProps): JSX.Element {
  const [files, setFiles] = useState<SandboxFileSummary[]>([]);
  const [systemVars, setSystemVars] = useState<SystemVariable[]>([]);
  const [userVars, setUserVars] = useState<TemplateVarSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!upstreamId) return;
    setLoading(true);
    setError(null);
    try {
      const [fileList, sysList, userList] = await Promise.all([
        listSandboxFiles(upstreamId),
        listSystemVariables("stdio"),
        listTemplateVars(upstreamId),
      ]);
      setFiles(fileList);
      setSystemVars(sysList);
      setUserVars(userList);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [upstreamId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Modal state. ``modalName`` is the URL-safe storage key; on
  // Add it's auto-derived from ``modalDisplayName`` at save time
  // (the operator never types it). On Replace it's fixed to the
  // existing file's id.
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<"add" | "replace">("add");
  const [modalName, setModalName] = useState("");
  const [modalDisplayName, setModalDisplayName] = useState("");
  const [modalTargetPath, setModalTargetPath] = useState("");
  const [modalContents, setModalContents] = useState("");
  const [modalFileName, setModalFileName] = useState<string | null>(null);
  const [modalError, setModalError] = useState<string | null>(null);
  const [modalSaving, setModalSaving] = useState(false);

  // Mirror of ``handleSave``'s validation guards so the Save button
  // can be disabled rather than producing a click-time error toast.
  // Keep the two in sync — handleSave still re-validates as a
  // belt-and-braces backstop.
  const canSave =
    !modalSaving &&
    modalDisplayName.trim().length > 0 &&
    modalTargetPath.length > 0 &&
    (modalTargetPath.startsWith("/") || modalTargetPath.startsWith("${")) &&
    (modalMode !== "add" || modalContents.length > 0) &&
    modalContents.length <= MAX_FILE_BYTES;
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const openAdd = useCallback(() => {
    setModalMode("add");
    setModalName("");
    setModalDisplayName("");
    setModalTargetPath("");
    setModalContents("");
    setModalFileName(null);
    setModalError(null);
    setModalOpen(true);
  }, []);

  const openReplace = useCallback((file: SandboxFileSummary) => {
    setModalMode("replace");
    setModalName(file.name);
    setModalDisplayName(file.display_name);
    setModalTargetPath(file.target_path);
    setModalContents("");
    setModalFileName(null);
    setModalError(null);
    setModalOpen(true);
  }, []);

  const closeModal = useCallback(() => {
    setModalOpen(false);
    setModalSaving(false);
  }, []);

  const handleFilePicked = useCallback(
    async (file: File): Promise<void> => {
      if (file.size > MAX_FILE_BYTES) {
        setModalError(
          `File too large: ${bytesToHuman(file.size)} exceeds ${bytesToHuman(MAX_FILE_BYTES)} cap`,
        );
        return;
      }
      try {
        const text = await file.text();
        setModalContents(text);
        setModalFileName(file.name);
        setModalError(null);
        // Auto-suggest a target_path on Add when the operator hasn't
        // typed one yet — preserves whatever they typed.
        setModalTargetPath((prev) => prev || `\${HOME}/${file.name}`);
        // Same for the display name: default to the uploaded
        // filename (more readable than a slugified id) but never
        // overwrite an explicit operator entry.
        setModalDisplayName((prev) => prev || file.name);
      } catch (e) {
        setModalError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  const onDrop = useCallback(
    async (e: React.DragEvent<HTMLDivElement>): Promise<void> => {
      e.preventDefault();
      const file = e.dataTransfer.files?.[0];
      if (file) await handleFilePicked(file);
    },
    [handleFilePicked],
  );

  const onDragOver = useCallback((e: React.DragEvent<HTMLDivElement>): void => {
    e.preventDefault();
  }, []);

  const handleSave = useCallback(async () => {
    if (!modalDisplayName.trim()) {
      setModalError("Display name is required.");
      return;
    }
    if (!modalTargetPath) {
      setModalError("Target path is required.");
      return;
    }
    if (
      !modalTargetPath.startsWith("/") &&
      !modalTargetPath.startsWith("${")
    ) {
      setModalError(
        "Target path must be absolute or start with a ${...} reference (e.g. ${HOME}/...).",
      );
      return;
    }
    if (modalMode === "add" && !modalContents) {
      setModalError("Pick a file to upload (or paste contents below).");
      return;
    }
    if (modalContents.length > MAX_FILE_BYTES) {
      setModalError(
        `Contents too large: ${bytesToHuman(modalContents.length)} exceeds ${bytesToHuman(MAX_FILE_BYTES)} cap`,
      );
      return;
    }
    // On Add: derive a fresh URL-safe id from the display name
    // (uniquified against existing files). On Replace: the id is
    // fixed to the file's existing storage key — operators rename
    // by editing display_name only.
    const fileId =
      modalMode === "add"
        ? makeUniqueFileId(modalDisplayName, files.map((f) => f.name))
        : modalName;
    setModalSaving(true);
    try {
      // Replace with no new contents → keep existing body, only
      // bump the display_name / target_path. Server-side ``set``
      // accepts the same shape for both create and replace; we
      // send the existing body as fetched contents only when
      // contents were re-uploaded.
      const payload = modalContents;
      await setSandboxFile(
        upstreamId,
        fileId,
        payload,
        modalTargetPath,
        modalDisplayName,
      );
      await refresh();
      onFilesChanged?.();
      closeModal();
    } catch (e) {
      setModalError(e instanceof Error ? e.message : String(e));
      setModalSaving(false);
    }
  }, [
    files,
    modalContents,
    modalDisplayName,
    modalMode,
    modalName,
    modalTargetPath,
    refresh,
    upstreamId,
    closeModal,
    onFilesChanged,
  ]);

  const { confirm, dialogProps } = useConfirm();

  const handleDelete = useCallback(
    async (name: string): Promise<void> => {
      const ok = await confirm({
        title: `Are you sure you want to remove ${name}?`,
        confirmLabel: "Delete",
        cancelLabel: "Cancel",
        destructive: true,
      });
      if (!ok) return;
      try {
        await deleteSandboxFile(upstreamId, name);
        await refresh();
        onFilesChanged?.();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [confirm, refresh, upstreamId, onFilesChanged],
  );

  // Live unknown-variable detection for the in-flight target path.
  // Mirrors the parent's amber callout (which only re-scans after the
  // modal closes and the file list refetches) so the operator sees
  // typos as they type, right next to the field. Distinct names only.
  const inflightUnresolvedNames = useMemo<string[]>(() => {
    if (!modalTargetPath) return [];
    const refs = findReferencesInSandboxFiles([
      { name: "_in_flight_", target_path: modalTargetPath },
    ]);
    const definedNames = [
      ...systemVars.map((v) => v.name),
      ...userVars.map((v) => v.name),
    ];
    const { unresolved } = analyseReferences(refs, definedNames);
    const seen = new Set<string>();
    const out: string[] = [];
    for (const u of unresolved) {
      if (!seen.has(u.name)) {
        seen.add(u.name);
        out.push(u.name);
      }
    }
    return out;
  }, [modalTargetPath, systemVars, userVars]);

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Files
          </span>
          <p className="mt-0.5 text-xs text-zinc-500">
            Files written into the sandbox at launch. Use{" "}
            <code>{'${HOME}'}</code> in the target path to keep it portable.
          </p>
        </div>
        {!readOnly && (
          <button
            type="button"
            onClick={openAdd}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded shrink-0"
          >
            <Plus size={12} /> Add file
          </button>
        )}
      </div>

      {loading && <div className="text-xs text-zinc-500">Loading…</div>}
      {error && (
        <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
          {error}
        </div>
      )}

      {!loading && files.length === 0 && (
        <div className="text-xs text-zinc-500 italic">
          No files defined.
        </div>
      )}

      {files.length > 0 && (
        <div className="border border-zinc-200 rounded">
          <table className="w-full text-xs">
            <thead className="bg-zinc-50 text-zinc-600">
              <tr>
                <th className="text-left px-3 py-1.5 font-medium">Display name</th>
                <th className="text-left px-3 py-1.5 font-medium">Target path</th>
                <th className="text-left px-3 py-1.5 font-medium">Size</th>
                <th className="text-left px-3 py-1.5 font-medium">Sha256</th>
                <th className="text-left px-3 py-1.5 font-medium">Modified</th>
                <th className="px-3 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => {
                return (
                  <tr key={f.name} className="border-t border-zinc-100">
                    <td className="px-3 py-1.5">
                      <div className="flex items-center gap-1">
                        <FileText className="w-3 h-3 text-zinc-400" />
                        <span className="text-zinc-800">{f.display_name}</span>
                      </div>
                    </td>
                    <td className="px-3 py-1.5 font-mono text-zinc-700">
                      <div className="text-zinc-500">{f.target_path}</div>
                    </td>
                    <td className="px-3 py-1.5 text-zinc-600">
                      {bytesToHuman(f.size_bytes)}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-zinc-500">
                      {f.sha256.slice(0, SHA_DISPLAY_CHARS)}
                    </td>
                    <td className="px-3 py-1.5 text-zinc-500">
                      {new Date(f.updated_at).toLocaleString()}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      {!readOnly && (
                        <div className="flex items-center justify-end gap-1">
                          <button
                            type="button"
                            onClick={() => openReplace(f)}
                            className="text-xs px-2 py-0.5 border border-zinc-300 rounded hover:bg-zinc-50"
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleDelete(f.name)}
                            className="text-zinc-500 hover:text-red-600"
                            title="Delete"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Add / Replace modal */}
      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="bg-white rounded-lg shadow-lg max-w-lg w-full p-4 space-y-3">
            <h3 className="text-sm font-semibold">
              {modalMode === "add"
                ? "Add Sandbox file"
                : `Edit ${modalDisplayName}`}
            </h3>

            <div>
              <label className="block text-xs text-zinc-600 mb-1">
                Display name
              </label>
              <input
                type="text"
                value={modalDisplayName}
                onChange={(e) => setModalDisplayName(e.target.value)}
                placeholder="GCP service account"
                className="w-full px-2 py-1 text-sm border border-zinc-300 rounded"
              />
            </div>

            <div>
              <label className="block text-xs text-zinc-600 mb-1">
                Target path
              </label>
              <input
                type="text"
                value={modalTargetPath}
                onChange={(e) => setModalTargetPath(e.target.value)}
                placeholder="${HOME}/.config/gcloud/credentials.json"
                className="w-full px-2 py-1 text-sm border border-zinc-300 rounded font-mono"
              />
              {inflightUnresolvedNames.length > 0 && (
                <div
                  className="mt-1 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1"
                  role="alert"
                >
                  Unknown variable
                  {inflightUnresolvedNames.length > 1 ? "s" : ""}:{" "}
                  {inflightUnresolvedNames.map((n, i) => (
                    <span key={n}>
                      {i > 0 && ", "}
                      <code>{`\${${n}}`}</code>
                    </span>
                  ))}
                  . Define{" "}
                  {inflightUnresolvedNames.length > 1 ? "them" : "it"}{" "}
                  in the Variables card before the next session start.
                </div>
              )}
            </div>

            <div>
              <label className="block text-xs text-zinc-600 mb-1">
                {modalMode === "add" ? "Contents" : "Replace contents (optional)"}
              </label>
              <div
                onDrop={(e) => void onDrop(e)}
                onDragOver={onDragOver}
                className="border-2 border-dashed border-zinc-300 rounded p-3 text-center text-xs text-zinc-500 hover:border-zinc-400 cursor-pointer"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="w-4 h-4 mx-auto text-zinc-400 mb-1" />
                {modalFileName ? (
                  <span>
                    Loaded <span className="font-mono">{modalFileName}</span> (
                    {bytesToHuman(modalContents.length)})
                  </span>
                ) : (
                  <span>Drop a file here or click to upload</span>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void handleFilePicked(f);
                }}
                className="hidden"
              />
            </div>

            {modalError && (
              <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
                {modalError}
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={closeModal}
                className="px-3 py-1 text-xs border border-zinc-300 rounded hover:bg-zinc-50"
                disabled={modalSaving}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleSave()}
                className="px-3 py-1 text-xs font-medium bg-zinc-900 text-white rounded hover:bg-zinc-800 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-zinc-900"
                disabled={!canSave}
              >
                {modalSaving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog {...dialogProps} />
    </div>
  );
}
