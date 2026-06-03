import { useRef, useState } from "react";
import { X, Upload, Check, AlertCircle, Pencil, HelpCircle } from "lucide-react";
import { confirmImport, previewImport } from "../api/admin";
import type { ImportEntry, ImportPreviewResponse, ImportResultResponse } from "../api/types";
import { useTranslation } from "../i18n/index";
import { maybeHandlePlanLimit } from "../lib/planLimits";
import IdInput from "./ui/id-input";
import { Tooltip, TooltipTrigger, TooltipContent } from "./ui/tooltip";
import {
  buildConfirmEntries,
  computeIdErrors,
  defaultIds,
  defaultSelectedKeys,
  groupEntries,
  normalizePreviewResponse,
  normalizeResultResponse,
  rowKey,
  type ImportGroup,
} from "../lib/importMcp";

type Step = "file-select" | "preview" | "importing" | "results";

interface Props {
  open: boolean;
  onClose: () => void;
  onComplete: () => void;
}

/** Friendly transport label — never expose the raw `streamable_http` token. */
function transportLabel(transport: string): string {
  return transport === "stdio" ? "stdio" : "HTTP";
}

export function ImportMcpModal({ open, onClose, onComplete }: Props) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState<Step>("file-select");
  const [rawData, setRawData] = useState<Record<string, unknown> | null>(null);
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [ids, setIds] = useState<Map<string, string>>(new Map());
  const [editing, setEditing] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<ImportResultResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const reset = () => {
    setStep("file-select");
    setRawData(null);
    setPreview(null);
    setSelected(new Set());
    setIds(new Map());
    setEditing(new Set());
    setResult(null);
    setError(null);
  };

  const handleClose = () => {
    if (result && result.added.length > 0) onComplete();
    else onClose();
    reset();
  };

  const processFile = async (text: string) => {
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError(t("import.invalidJson"));
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setError(t("import.invalidJson"));
      return;
    }
    try {
      // Normalize at the boundary so a missing/null/malformed list field
      // from the backend can never throw downstream (the prod crash class).
      const res = normalizePreviewResponse(await previewImport(parsed));
      setRawData(parsed);
      setPreview(res);
      setSelected(defaultSelectedKeys(res.entries));
      setIds(defaultIds(res.entries));
      setEditing(new Set());
      if (res.entries.length === 0 && res.parse_errors.length === 0) {
        setError(t("import.noEntries"));
        return;
      }
      setStep("preview");
    } catch (e) {
      setError(e instanceof Error ? e.message : t("import.previewFailed"));
    }
  };

  const readFile = (file: File | undefined) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => processFile(reader.result as string);
    reader.readAsText(file);
  };

  const handleConfirm = async () => {
    if (!rawData || !preview || selected.size === 0) return;
    setStep("importing");
    try {
      const entries = buildConfirmEntries(preview.entries, selected, ids);
      const res = normalizeResultResponse(await confirmImport(rawData, entries));
      setResult(res);
      setStep("results");
    } catch (e) {
      if (maybeHandlePlanLimit(e, { source: "import_mcp_confirm" })) {
        setStep("preview");
        onClose();
        return;
      }
      setError(e instanceof Error ? e.message : t("import.confirmFailed"));
      setStep("preview");
    }
  };

  const groups: ImportGroup[] = preview ? groupEntries(preview.entries) : [];
  const topGroup = groups.find((g) => g.scope !== "project");
  const projectGroups = groups.filter((g) => g.scope === "project");
  const existingIds = new Set(preview?.existing_ids ?? []);
  const idErrors = computeIdErrors(selected, ids, existingIds);
  const hasIdErrors = idErrors.size > 0;

  const selectableKeys = (entries: ImportEntry[]) =>
    entries.filter((e) => !e.blocked).map(rowKey);
  const groupAllSelected = (entries: ImportEntry[]) => {
    const keys = selectableKeys(entries);
    return keys.length > 0 && keys.every((k) => selected.has(k));
  };
  const groupSomeSelected = (entries: ImportEntry[]) => {
    const keys = selectableKeys(entries);
    return keys.some((k) => selected.has(k)) && !groupAllSelected(entries);
  };
  const toggleGroup = (entries: ImportEntry[]) => {
    const keys = selectableKeys(entries);
    const allOn = keys.length > 0 && keys.every((k) => selected.has(k));
    setSelected((prev) => {
      const next = new Set(prev);
      for (const k of keys) {
        if (allOn) next.delete(k);
        else next.add(k);
      }
      return next;
    });
  };

  const toggleRow = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const setId = (key: string, value: string) => {
    setIds((prev) => new Map(prev).set(key, value));
  };

  const toggleEdit = (key: string) => {
    setEditing((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const idErrorText = (code: string): string =>
    code === "empty"
      ? t("import.idEmpty")
      : code === "exists"
        ? t("import.idExists")
        : t("import.idDuplicate");

  const renderRow = (entry: ImportEntry) => {
    const key = rowKey(entry);
    const isSelected = selected.has(key);
    const idErr = isSelected ? idErrors.get(key) : undefined;
    const isEditing = editing.has(key);
    const currentId = ids.get(key) ?? entry.proposed_id;
    return (
      <tr key={key} className={entry.blocked ? "opacity-50" : "hover:bg-zinc-50"}>
        {/* Every cell's primary content sits in the same h-7 centered
            line, so the id box (the tallest element) doesn't push its
            text out of alignment with the other columns. Sub-lines hang
            below the id only and never nudge the other cells. */}
        <td className="px-3 py-2 align-top">
          <div className="flex items-center h-7">
            <input
              type="checkbox"
              checked={isSelected}
              disabled={entry.blocked}
              onChange={() => toggleRow(key)}
              className="rounded"
            />
          </div>
        </td>
        <td className="px-3 py-2 align-top">
          <div className="flex items-center gap-1 h-7">
            {isEditing ? (
              <IdInput
                value={currentId}
                onChange={(v) => setId(key, v)}
                autoFocus
                onKeyDown={(e) => { if (e.key === "Enter") toggleEdit(key); }}
                className="w-44 -ml-[7px] px-1.5 py-0.5 text-xs font-mono leading-5"
              />
            ) : (
              // Same box as the input (width / padding / line-height +
              // a transparent border) so toggling edit only flips the
              // border color and never reflows the column. The -ml-[7px]
              // cancels the box's left padding + border so the text sits
              // flush under the "ID" header label.
              <span
                title={currentId}
                className="block w-44 -ml-[7px] px-1.5 py-0.5 text-xs font-mono leading-5 border border-transparent rounded truncate"
              >
                {currentId}
              </span>
            )}
            {!entry.blocked && (
              <button
                type="button"
                onClick={() => toggleEdit(key)}
                aria-label={t("import.editId")}
                className="text-zinc-400 hover:text-zinc-600 shrink-0"
              >
                {isEditing ? <Check size={12} /> : <Pencil size={12} />}
              </button>
            )}
          </div>
          {entry.blocked && entry.blocked_reason && (
            <div className="text-xs text-red-500">{entry.blocked_reason}</div>
          )}
          {entry.duplicate_of && (
            <div className="text-xs text-amber-600">
              {t("import.duplicateBadge", { id: entry.duplicate_of.proposed_id })}
            </div>
          )}
          {idErr && <div className="text-xs text-red-600">{idErrorText(idErr)}</div>}
        </td>
        <td className="px-3 py-2 align-top">
          <div className="flex items-center h-7">{entry.display_name}</div>
        </td>
        <td className="px-3 py-2 align-top text-xs">
          <div className="flex items-center h-7">
            <span className="px-1.5 py-0.5 rounded bg-zinc-100 text-zinc-600">
              {transportLabel(entry.transport)}
            </span>
          </div>
        </td>
        <td className="px-3 py-2 align-top text-xs">
          <div className="flex items-center h-7">
            <span className="px-1.5 py-0.5 rounded bg-zinc-100 text-zinc-600">
              {entry.auth_mode}
            </span>
          </div>
        </td>
      </tr>
    );
  };

  const renderTable = (
    entries: ImportEntry[],
    headerSelectAll: React.ReactNode,
  ) => (
    <div className="border border-zinc-200 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-zinc-50 text-zinc-600">
          <tr>
            <th className="px-3 py-2 w-8">{headerSelectAll}</th>
            <th className="text-left px-3 py-2 font-medium">
              <span className="inline-flex items-center gap-1">
                {t("import.headerId")}
                <Tooltip>
                  <TooltipTrigger className="cursor-help text-zinc-400 hover:text-zinc-600">
                    <HelpCircle size={12} />
                  </TooltipTrigger>
                  <TooltipContent>{t("import.idHelp")}</TooltipContent>
                </Tooltip>
              </span>
            </th>
            <th className="text-left px-3 py-2 font-medium">{t("import.headerName")}</th>
            <th className="text-left px-3 py-2 font-medium">{t("import.headerTransport")}</th>
            <th className="text-left px-3 py-2 font-medium">{t("import.headerAuth")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">{entries.map(renderRow)}</tbody>
      </table>
    </div>
  );

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={handleClose} />
      <div className="relative bg-white rounded-lg shadow-lg w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200">
          <h3 className="text-lg font-semibold text-zinc-900">{t("import.title")}</h3>
          <button onClick={handleClose} className="text-zinc-400 hover:text-zinc-600">
            <X size={16} />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-4 overflow-y-auto flex-1">
          {step === "file-select" && (
            <div>
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); readFile(e.dataTransfer.files[0]); }}
                onClick={() => fileRef.current?.click()}
                className={`border-2 border-dashed rounded-lg p-12 text-center cursor-pointer transition-colors ${
                  dragOver ? "border-blue-400 bg-blue-50" : "border-zinc-300 hover:border-zinc-400"
                }`}
              >
                <Upload className="mx-auto mb-3 text-zinc-400" size={32} />
                <p className="text-sm text-zinc-600">{t("import.dropzone")}</p>
                <p className="text-xs text-zinc-400 mt-1">{t("import.dropzoneHint")}</p>
              </div>
              <input
                ref={fileRef}
                type="file"
                accept=".json"
                onChange={(e) => readFile(e.target.files?.[0])}
                className="hidden"
              />
              {error && (
                <p className="mt-3 text-sm text-red-600 flex items-center gap-1">
                  <AlertCircle size={14} /> {error}
                </p>
              )}
            </div>
          )}

          {step === "preview" && preview && (
            <div className="space-y-5">
              {/* User-level / standard MCPs — classic table, no title. */}
              {topGroup && topGroup.entries.length > 0 && renderTable(
                topGroup.entries,
                <input
                  type="checkbox"
                  ref={(el) => { if (el) el.indeterminate = groupSomeSelected(topGroup.entries); }}
                  checked={groupAllSelected(topGroup.entries)}
                  onChange={() => toggleGroup(topGroup.entries)}
                  className="rounded"
                />,
              )}

              {/* One section per project: checkbox + name + greyed full path. */}
              {projectGroups.map((group) => (
                <div key={`${group.scope}:${group.projectPath ?? ""}`} className="space-y-1.5">
                  <div className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      aria-label={t("import.selectGroup")}
                      ref={(el) => { if (el) el.indeterminate = groupSomeSelected(group.entries); }}
                      checked={groupAllSelected(group.entries)}
                      disabled={selectableKeys(group.entries).length === 0}
                      onChange={() => toggleGroup(group.entries)}
                      className="rounded mt-1"
                    />
                    <div className="min-w-0 flex items-baseline gap-2">
                      <span className="text-sm font-medium text-zinc-800 shrink-0">{group.label}</span>
                      {group.projectPath && (
                        <span className="text-xs text-zinc-400 truncate" title={group.projectPath}>
                          {group.projectPath}
                        </span>
                      )}
                    </div>
                  </div>
                  {renderTable(group.entries, null)}
                </div>
              ))}

              {preview.parse_errors.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium text-red-600 mb-1">
                    {t("import.parseErrors")}
                  </h4>
                  <ul className="text-xs text-red-500 space-y-0.5">
                    {preview.parse_errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                </div>
              )}

              {preview.entries.length === 0 && (
                <p className="text-sm text-zinc-500">{t("import.noEntries")}</p>
              )}

              {error && (
                <p className="text-sm text-red-600 flex items-center gap-1">
                  <AlertCircle size={14} /> {error}
                </p>
              )}
            </div>
          )}

          {step === "importing" && (
            <div className="py-8 text-center">
              <p className="text-zinc-500">{t("import.importing")}</p>
            </div>
          )}

          {step === "results" && result && (
            <div className="space-y-3">
              {result.added.length > 0 && (
                <div className="flex items-start gap-2">
                  <Check size={16} className="text-green-600 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-sm font-medium text-green-700">
                      {t("import.resultAdded", { count: result.added.length })}
                    </p>
                    <p className="text-xs text-zinc-500">{result.added.join(", ")}</p>
                  </div>
                </div>
              )}
              {result.skipped.length > 0 && (
                <div className="flex items-start gap-2">
                  <AlertCircle size={16} className="text-zinc-400 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-sm text-zinc-500">
                      {t("import.resultSkipped", { count: result.skipped.length })}
                    </p>
                    <p className="text-xs text-zinc-400">{result.skipped.join(", ")}</p>
                  </div>
                </div>
              )}
              {result.errors.length > 0 && (
                <div className="flex items-start gap-2">
                  <AlertCircle size={16} className="text-red-500 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-sm font-medium text-red-600">
                      {t("import.resultErrors", { count: result.errors.length })}
                    </p>
                    {result.errors.map((err) => (
                      <p key={err.id} className="text-xs text-red-500">
                        {err.id}: {err.error}
                      </p>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-zinc-200 flex items-center justify-end gap-2">
          {step === "preview" && preview && preview.entries.length > 0 && (
            <>
              {hasIdErrors && (
                <span className="mr-auto text-xs text-red-600 flex items-center gap-1">
                  <AlertCircle size={12} /> {t("import.fixIds")}
                </span>
              )}
              <button
                onClick={() => { reset(); }}
                className="px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 rounded"
              >
                {t("common.cancel")}
              </button>
              <button
                onClick={handleConfirm}
                disabled={selected.size === 0 || hasIdErrors}
                className="px-3 py-1.5 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded disabled:opacity-50"
              >
                {t("import.confirm", { count: selected.size })}
              </button>
            </>
          )}
          {step === "results" && (
            <button
              onClick={handleClose}
              className="px-3 py-1.5 text-sm text-white bg-zinc-900 hover:bg-zinc-800 rounded"
            >
              {t("import.done")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
