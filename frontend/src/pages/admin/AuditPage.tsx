import { useEffect, useRef, useState, useCallback } from "react";
import { fetchAuditFilters, searchAudit } from "../../api/admin";
import type { AuditFilterValues } from "../../api/admin";
import type { AuditSearchResponse } from "../../api/types";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import { useTranslation } from "../../i18n/index";
import { useEventSource } from "../../hooks/useEventSource";

const ACTION_OPTIONS = [
  { value: "", labelKey: "audit.filterAllActions" },
  { value: "tool_call", labelKey: "audit.filterToolCall" },
  { value: "client_connect,client_disconnect", labelKey: "audit.filterConnection" },
] as const;

function Combobox({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder: string;
}) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const filtered = value
    ? options.filter((o) => o.toLowerCase().includes(value.toLowerCase()))
    : options;

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Reset active index when list changes
  useEffect(() => {
    setActiveIndex(-1);
  }, [value, open]);

  // Scroll active item into view
  useEffect(() => {
    if (activeIndex < 0 || !listRef.current) return;
    const items = listRef.current.children;
    items[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      setOpen(true);
      e.preventDefault();
      return;
    }
    if (!open || filtered.length === 0) return;

    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActiveIndex((prev) => (prev < filtered.length - 1 ? prev + 1 : 0));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActiveIndex((prev) => (prev > 0 ? prev - 1 : filtered.length - 1));
        break;
      case "Enter":
        e.preventDefault();
        if (activeIndex >= 0) {
          onChange(filtered[activeIndex]);
          setOpen(false);
        }
        break;
      case "Escape":
        setOpen(false);
        break;
    }
  };

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <input
          type="text"
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          className={`px-3 py-1.5 border border-zinc-200 rounded text-sm w-48 focus:outline-none focus:ring-1 focus:ring-zinc-400 ${value ? "pr-7" : ""}`}
        />
        {value && (
          <button
            onClick={() => { onChange(""); setOpen(false); }}
            className="absolute right-1.5 top-1/2 -translate-y-1/2 text-zinc-400 hover:text-zinc-600 p-0.5"
            tabIndex={-1}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M3 3l8 8M11 3l-8 8" />
            </svg>
          </button>
        )}
      </div>
      {open && filtered.length > 0 && (
        <div ref={listRef} className="absolute z-10 mt-1 w-full bg-white border border-zinc-200 rounded shadow-lg max-h-48 overflow-auto">
          {filtered.map((opt, i) => (
            <button
              key={opt}
              onClick={() => { onChange(opt); setOpen(false); }}
              className={`w-full text-left px-3 py-1.5 text-sm text-zinc-700 ${activeIndex === i ? "bg-zinc-100" : "hover:bg-zinc-50"}`}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function OutcomeBadge({ entry }: { entry: Record<string, unknown> }) {
  const action = String(entry.action ?? "tool_call");
  if (action === "tool_call") {
    const decision = String(entry.policy_decision ?? "—");
    const isAllowed = decision === "allowed";
    return (
      <div className="flex flex-col gap-0.5">
        <span
          className={`px-1.5 py-0.5 rounded text-xs font-medium inline-block w-fit ${
            isAllowed ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
          }`}
        >
          {decision}
        </span>
        {/* Deny reason (which MCP / which forbidden argument). Only
            denials carry an error_message on tool_call rows. */}
        {entry.error_message != null && (
          <span
            className="text-[10px] text-red-500 truncate max-w-[250px]"
            title={String(entry.error_message)}
          >
            {String(entry.error_message)}
          </span>
        )}
      </div>
    );
  }
  const outcome = String(entry.outcome ?? "—");
  const color =
    outcome === "success"
      ? "bg-green-100 text-green-700"
      : outcome === "pending"
        ? "bg-yellow-100 text-yellow-700"
        : outcome === "error"
          ? "bg-red-100 text-red-700"
          : "bg-zinc-100 text-zinc-600";
  return (
    <div className="flex flex-col gap-0.5">
      <span className={`px-1.5 py-0.5 rounded text-xs font-medium inline-block w-fit ${color}`}>
        {outcome}
      </span>
      {entry.error_message != null && (
        <span className="text-[10px] text-red-500 truncate max-w-[250px]" title={String(entry.error_message)}>
          {String(entry.error_message)}
        </span>
      )}
    </div>
  );
}

function ActionBadge({ action }: { action: string }) {
  const color =
    action === "tool_call"
      ? "bg-blue-100 text-blue-700"
      : action === "client_connect"
        ? "bg-green-100 text-green-700"
        : action === "client_disconnect"
          ? "bg-zinc-100 text-zinc-600"
          : "bg-zinc-100 text-zinc-600";
  const label =
    action === "client_connect"
      ? "connect"
      : action === "client_disconnect"
        ? "disconnect"
        : action;
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${color}`}>
      {label}
    </span>
  );
}

function DetailCell({ entry }: { entry: Record<string, unknown> }) {
  const action = String(entry.action ?? "tool_call");
  if (action === "tool_call") {
    return <span className="font-mono text-xs">{String(entry.tool ?? "—")}</span>;
  }
  if (entry.client_type) {
    return <span className="text-xs text-zinc-600">{String(entry.client_type)}</span>;
  }
  return <span className="text-xs text-zinc-500">—</span>;
}

const ENTRY_LIMIT = 100;

function matchesFilters(
  entry: Record<string, unknown>,
  f: { user_id: string; mcp_id: string; tool: string; action: string },
): boolean {
  if (f.user_id && entry.user_id !== f.user_id) return false;
  if (f.mcp_id && entry.upstream_id !== f.mcp_id) return false;
  if (f.tool && !String(entry.tool ?? "").toLowerCase().includes(f.tool.toLowerCase())) return false;
  if (f.action) {
    const actions = f.action.split(",").filter(Boolean);
    if (!actions.includes(String(entry.action ?? "tool_call"))) return false;
  }
  return true;
}

export function AuditPage() {
  const { t } = useTranslation();
  const [data, setData] = useState<AuditSearchResponse>({
    entries: [],
    count: 0,
  });
  const [filterValues, setFilterValues] = useState<AuditFilterValues>({
    user_ids: [],
    upstream_ids: [],
  });
  const [filters, setFilters] = useState({
    user_id: "",
    mcp_id: "",
    tool: "",
    action: "",
  });
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const tableRef = useRef<HTMLDivElement>(null);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const doSearch = (f: typeof filters, { silent = false } = {}) => {
    if (!silent) setLoading(true);
    searchAudit({
      user_id: f.user_id || undefined,
      mcp_id: f.mcp_id || undefined,
      tool: f.tool || undefined,
      action: f.action || undefined,
      limit: ENTRY_LIMIT,
    })
      .then((result) => {
        setData(result);
      })
      .finally(() => { if (!silent) setLoading(false); });
  };

  const updateFilter = (patch: Partial<typeof filters>) => {
    setFilters((prev) => {
      const next = { ...prev, ...patch };
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => doSearch(next), 250);
      return next;
    });
  };

  useEffect(() => {
    doSearch(filters);
    fetchAuditFilters().then(setFilterValues);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Real-time SSE updates
  const handleAuditEntry = useCallback((data: unknown) => {
    if (!live) return;
    const event = data as { payload?: Record<string, unknown> };
    const entry = event?.payload;
    if (!entry) return;
    if (!matchesFilters(entry, filtersRef.current)) return;

    setData((prev) => {
      const entries = [entry, ...prev.entries].slice(0, ENTRY_LIMIT);
      return { entries, count: entries.length };
    });

    // Update filter value lists with new user/upstream IDs
    const userId = entry.user_id as string | undefined;
    const upstreamId = entry.upstream_id as string | undefined;
    setFilterValues((prev) => {
      let changed = false;
      const userIds = [...prev.user_ids];
      const upstreamIds = [...prev.upstream_ids];
      if (userId && !userIds.includes(userId)) { userIds.push(userId); userIds.sort(); changed = true; }
      if (upstreamId && !upstreamIds.includes(upstreamId)) { upstreamIds.push(upstreamId); upstreamIds.sort(); changed = true; }
      return changed ? { user_ids: userIds, upstream_ids: upstreamIds } : prev;
    });

    if (tableRef.current) {
      tableRef.current.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [live]);

  useEventSource({ audit_entry: handleAuditEntry }, live);

  return (
    <div>
      <h2 className="text-xl font-semibold text-zinc-900 mb-4">{t("audit.title")}</h2>

      <div className="flex gap-2 mb-4 flex-wrap items-center">
        <Tooltip>
          <TooltipTrigger
            onClick={() => setLive((v) => !v)}
            className={`flex items-center justify-center w-8 h-8 rounded border ${
              live
                ? "bg-green-50 text-green-700 border-green-200"
                : "bg-zinc-50 text-zinc-500 border-zinc-200"
            }`}
          >
            {live ? (
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
                <rect x="2" y="1" width="4" height="12" rx="1" />
                <rect x="8" y="1" width="4" height="12" rx="1" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
                <path d="M3 1.5v11l9-5.5z" />
              </svg>
            )}
          </TooltipTrigger>
          <TooltipContent>{live ? t("audit.pause") : t("audit.play")}</TooltipContent>
        </Tooltip>
        <Combobox
          value={filters.user_id}
          onChange={(v) => updateFilter({ user_id: v })}
          options={filterValues.user_ids}
          placeholder={t("audit.placeholderUserId")}
        />
        <Combobox
          value={filters.mcp_id}
          onChange={(v) => updateFilter({ mcp_id: v })}
          options={filterValues.upstream_ids}
          placeholder={t("audit.placeholderMcpId")}
        />
        <input
          type="text"
          placeholder={t("audit.placeholderTool")}
          value={filters.tool}
          onChange={(e) => updateFilter({ tool: e.target.value })}
          className="px-3 py-1.5 border border-zinc-200 rounded text-sm w-48 focus:outline-none focus:ring-1 focus:ring-zinc-400"
        />
        <select
          value={filters.action}
          onChange={(e) => updateFilter({ action: e.target.value })}
          className="px-3 py-1.5 border border-zinc-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-zinc-400"
        >
          {ACTION_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {t(opt.labelKey)}
            </option>
          ))}
        </select>
      </div>

      {loading ? (
        <p className="text-zinc-500">{t("common.loading")}</p>
      ) : (
        <>
          <p className="text-sm text-zinc-500 mb-2">
            {t("audit.entriesCount", { count: data.count })}
          </p>
          <div ref={tableRef} className="border border-zinc-200 rounded-lg overflow-auto max-h-[70vh]">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="bg-zinc-50 text-zinc-600 sticky top-0">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerTime")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerAction")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerUser")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerUpstream")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerTool")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("audit.headerOutcome")}</th>
                  <th className="text-right px-4 py-2 font-medium">{t("audit.headerLatency")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {data.entries.map((entry, i) => (
                  <tr key={i} className="hover:bg-zinc-50">
                    <td className="px-4 py-2 text-xs text-zinc-500 whitespace-nowrap">
                      {entry.timestamp
                        ? new Date(String(entry.timestamp)).toLocaleString()
                        : "—"}
                    </td>
                    <td className="px-4 py-2">
                      <ActionBadge action={String(entry.action ?? "tool_call")} />
                    </td>
                    <td className="px-4 py-2 text-zinc-700">
                      {String(entry.user_id ?? "—")}
                    </td>
                    <td className="px-4 py-2 text-zinc-600">
                      {String(entry.upstream_id ?? "—")}
                    </td>
                    <td className="px-4 py-2">
                      <DetailCell entry={entry} />
                    </td>
                    <td className="px-4 py-2">
                      <OutcomeBadge entry={entry} />
                    </td>
                    <td className="px-4 py-2 text-right text-zinc-500 text-xs">
                      {entry.latency_ms != null
                        ? t("common.latencyMs", { ms: Number(entry.latency_ms).toFixed(0) })
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.entries.length === 0 && (
              <p className="p-4 text-center text-zinc-400">
                {t("audit.noEntries")}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
