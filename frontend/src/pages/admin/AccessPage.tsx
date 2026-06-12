import React, { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import { ArrowLeft, ChevronDown, ChevronRight, Pencil, Plus, Trash2 } from "lucide-react";
import IdInput from "../../components/ui/id-input";
import { FieldHint } from "../../components/ui/field-hint";
import { ToolAnnotationBadges } from "../../components/ToolTable";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import {
  createRole,
  deleteRole,
  renameRole,
  fetchRoleAccess,
  fetchRoles,
  fetchUpstreams,
  fetchAllTools,
  setRoleAutoEnableNew,
  setRoleMcpAccessEntry,
  setRoleToolAccessEntry,
  removeRoleToolAccessEntry,
  setRoleToolFallbackEnabled,
  setRoleCategoryDefault,
  removeRoleCategoryDefault,
  setRoleArgumentConstraint,
  removeRoleArgumentConstraint,
} from "../../api/admin";
import type {
  ArgumentConstraint,
  RoleAccessInfo,
  RoleSummary,
  ToolAccessConfig,
  ToolInfo,
  UpstreamSummary,
} from "../../api/types";
import { useAuth } from "../../hooks/useAuth";
import { useTranslation } from "../../i18n/index";
import { handlePlanLimitError, maybeHandlePlanLimit } from "../../lib/planLimits";
import { PlanLimitError } from "../../api/client";
import { McpAccessTable, type McpAccessRow } from "../../components/admin/McpAccessTable";
import {
  ANNOTATION_LABELS,
  resolveToolDefault,
  toolFlags,
} from "../../components/admin/ToolAccessSection";
import SettingToggle from "../../components/ui/setting-toggle";
import CategoryToggle from "../../components/ui/category-toggle";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { SettingsCard, SettingsField } from "../../components/SettingsCard";

function RoleSection({
  role,
  upstreams,
  allTools,
  onChange,
  onDefaultEnabledChange,
  onToolsClick,
}: {
  role: RoleAccessInfo;
  upstreams: UpstreamSummary[];
  allTools: ToolInfo[];
  onChange: (roleName: string, mcpId: string, value: boolean) => void;
  onDefaultEnabledChange: (roleName: string, value: boolean) => void;
  onToolsClick?: (mcpId: string) => void;
}) {
  const { t } = useTranslation();
  const mcps = role.mcp_access.mcps;

  const toolCounts = new Map<string, number>();
  for (const tool of allTools) {
    toolCounts.set(tool.upstream_id, (toolCounts.get(tool.upstream_id) ?? 0) + 1);
  }

  const rows: McpAccessRow[] = upstreams.map((upstream) => ({
    mcpId: upstream.id,
    displayName: upstream.display_name,
    value: mcps[upstream.id] ?? false,
    toolCount: toolCounts.get(upstream.id) ?? 0,
  }));

  return (
    <>
    <h3 className="text-lg font-medium text-zinc-900 mb-3">MCP Permissions</h3>
    <div className="flex items-center justify-between rounded-lg border border-zinc-200 bg-zinc-50/50 px-4 py-3 mb-4">
      <div>
        <span className="text-sm font-medium text-zinc-700">Enable new MCPs by default</span>
        <p className="text-xs text-zinc-400 mt-0.5">Newly added MCP servers will be automatically accessible to this role.</p>
      </div>
      <SettingToggle
        value={role.mcp_access.auto_enable_new}
        onChange={(v) => onDefaultEnabledChange(role.name, v)}
      />
    </div>
    <McpAccessTable
      upstreams={upstreams}
      rows={rows}
      bordered={true}
      onChange={(mcpId, value) => onChange(role.name, mcpId, value)}
      onToolsClick={onToolsClick}
      emptyMessage={t("access.noMcps")}
    />
    </>
  );
}

function RoleSettings({
  role,
  allRoles,
  roleSummaries,
  onRename,
  onDelete,
  onEditNameChange,
  planBlocksDelete = false,
}: {
  role: RoleAccessInfo;
  allRoles: RoleAccessInfo[];
  roleSummaries: RoleSummary[];
  onRename: (oldName: string, newName: string) => Promise<void>;
  onDelete: (name: string) => Promise<void>;
  onEditNameChange: (name: string | null) => void;
  /** When true, suppress the delete button. Free plans can't
   *  recreate a role afterwards (``max_custom_roles=0``), so deleting
   *  the default ``user`` role would strand the org without a
   *  default — surfacing the gate at the delete site avoids that
   *  trap. */
  planBlocksDelete?: boolean;
}) {
  const { t } = useTranslation();
  const orgSlug = useOrgSlug();
  const { confirm, dialogProps } = useConfirm();
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(role.name);
  const [error, setError] = useState("");

  useEffect(() => {
    setEditing(false);
    setEditName(role.name);
    setError("");
  }, [role.name]);

  const summary = roleSummaries.find((r) => r.name === role.name);
  const userCount = summary?.user_count ?? 0;
  const serviceTokenCount = summary?.service_token_count ?? 0;
  const canDelete =
    userCount === 0 && serviceTokenCount === 0 && !planBlocksDelete;
  const sanitizedName = editName.toLowerCase().replace(/[^a-z0-9\-_.]/g, "");
  const nameConflict = sanitizedName !== role.name && allRoles.some((r) => r.name === sanitizedName);
  const canSave = !!sanitizedName && !nameConflict;

  const enterEdit = () => {
    setEditName(role.name);
    setError("");
    setEditing(true);
    onEditNameChange(role.name);
  };

  const cancelEdit = () => {
    setEditName(role.name);
    setError("");
    setEditing(false);
    onEditNameChange(null);
  };

  const handleSave = async () => {
    const sanitized = editName.toLowerCase().replace(/[^a-z0-9\-_.]/g, "");
    if (!sanitized) { setError("Name cannot be empty"); return; }
    try {
      setError("");
      if (sanitized !== role.name) await onRename(role.name, sanitized);
      setEditing(false);
      onEditNameChange(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    }
  };

  const handleRemove = async () => {
    const ok = await confirm({
      title: "Delete role",
      message: `Delete role "${role.name}"? This cannot be undone.`,
      confirmLabel: "Delete",
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    try {
      setError("");
      await onDelete(role.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    }
  };

  return (
    <div className="mb-6">
      {/* Settings section */}
      <SettingsCard
        title="Settings"
        actions={
          editing ? (
            <div className="flex items-center gap-2">
              <button
                onClick={handleSave}
                disabled={!canSave}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Save
              </button>
              <button
                onClick={cancelEdit}
                className="px-3 py-1.5 bg-zinc-100 text-zinc-700 text-sm rounded hover:bg-zinc-200"
              >
                {t("common.cancel")}
              </button>
            </div>
          ) : (
            <button
              onClick={enterEdit}
              className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
            >
              <Pencil size={14} /> Edit
            </button>
          )
        }
      >
        <SettingsField label="Name">
          {editing ? (
            <>
              <IdInput
                value={editName}
                onChange={(v) => { setEditName(v); onEditNameChange(v); }}
                placeholder="role-name"
                className="w-48"
              />
              {nameConflict && <p className="mt-1 text-xs text-red-500">A role with this name already exists</p>}
              {error && <p className="mt-1 text-xs text-red-500">{error}</p>}
            </>
          ) : (
            <span className="text-sm text-zinc-900 font-medium">{role.name}</span>
          )}
        </SettingsField>
      </SettingsCard>

      {editing && (
        <div className="mb-4 flex justify-end">
          {canDelete ? (
            <button
              onClick={handleRemove}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-red-500 border border-red-200 rounded hover:bg-red-50 hover:border-red-300"
            >
              <Trash2 size={12} />
              Delete
            </button>
          ) : (
            <Tooltip>
              <TooltipTrigger className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-zinc-300 border border-zinc-200 rounded cursor-not-allowed">
                <Trash2 size={12} />
                Delete
              </TooltipTrigger>
              <TooltipContent>
                {planBlocksDelete
                  ? "Free plans can't recreate a role after deleting it. Upgrade to Team to remove this restriction."
                  : `Cannot delete: ${[
                      userCount > 0
                        ? `${userCount} team member${userCount > 1 ? "s" : ""}`
                        : null,
                      serviceTokenCount > 0
                        ? `${serviceTokenCount} service token${serviceTokenCount > 1 ? "s" : ""}`
                        : null,
                    ]
                      .filter(Boolean)
                      .join(" and ")} assigned`}
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      )}

      {/* Team members + service tokens */}
      <div className="text-sm space-y-2 mb-4">
        <div className="flex items-center gap-2">
          <span className="text-zinc-500">Assigned to:</span>
          <Link
            to={`/orgs/${orgSlug}/admin/team?role=${encodeURIComponent(role.name)}`}
            className="text-blue-500 hover:underline"
          >
            {userCount === 1 ? "1 team member" : `${userCount} team members`}
          </Link>
          {serviceTokenCount > 0 && (
            <>
              <span className="text-zinc-400">and</span>
              <Link
                to={`/orgs/${orgSlug}/admin/service-tokens`}
                className="text-blue-500 hover:underline"
              >
                {serviceTokenCount === 1
                  ? "1 service token"
                  : `${serviceTokenCount} service tokens`}
              </Link>
            </>
          )}
        </div>
      </div>

      <ConfirmDialog {...dialogProps} />
    </div>
  );
}

/** Categorize a tool into its most restrictive annotation group.
 * Priority: destructive > openWorld > readOnly > idempotent > uncategorized */
const CATEGORY_PRIORITY = ["readOnly", "destructive"] as const;

function categorizeTools(tools: ToolInfo[]): {
  categories: { key: string; label: string; tools: ToolInfo[] }[];
  uncategorized: ToolInfo[];
} {
  const buckets = new Map<string, ToolInfo[]>();
  const uncategorized: ToolInfo[] = [];

  for (const tool of tools) {
    const flags = toolFlags(tool);
    let placed = false;
    for (const cat of CATEGORY_PRIORITY) {
      if (flags[cat]) {
        const list = buckets.get(cat) ?? [];
        list.push(tool);
        buckets.set(cat, list);
        placed = true;
        break;
      }
    }
    if (!placed) uncategorized.push(tool);
  }

  const categories: { key: string; label: string; tools: ToolInfo[] }[] = [];
  for (const cat of CATEGORY_PRIORITY) {
    const list = buckets.get(cat);
    if (list && list.length > 0) {
      categories.push({ key: cat, label: ANNOTATION_LABELS[cat] ?? cat, tools: list });
    }
  }
  return { categories, uncategorized };
}


type DraftConstraint = { mode: "allow" | "forbid" | ""; pattern: string };

function ConstraintRow({
  argName,
  argType,
  draft,
  editing,
  error,
  onDraftChange,
}: {
  argName: string;
  argType: string;
  draft: DraftConstraint;
  editing: boolean;
  error: string;
  onDraftChange: (d: DraftConstraint) => void;
}) {
  return (
    <div className="flex items-center gap-3 py-1.5">
      <div className="w-40 shrink-0">
        <span className="font-mono text-xs text-zinc-700">{argName}</span>
        <span className="text-xs text-zinc-400 ml-1.5">{argType}</span>
      </div>
      {editing ? (
        <>
          <select
            value={draft.mode}
            onChange={(e) => onDraftChange({ ...draft, mode: e.target.value as DraftConstraint["mode"] })}
            className="px-1.5 py-1 text-xs border border-zinc-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
          >
            <option value="">No check</option>
            <option value="allow">Allow</option>
            <option value="forbid">Forbid</option>
          </select>
          {draft.mode && (
            <input
              type="text"
              value={draft.pattern}
              onChange={(e) => onDraftChange({ ...draft, pattern: e.target.value })}
              placeholder={draft.mode === "allow" ? "e.g. ^SELECT\\s" : "e.g. DROP|DELETE"}
              className="flex-1 px-2 py-1 text-xs font-mono border border-zinc-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
            />
          )}
          {error && error !== "empty" && <span className="shrink-0"><FieldHint inline error>{error}</FieldHint></span>}
        </>
      ) : (
        draft.mode ? (
          <>
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
              draft.mode === "allow" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
            }`}>
              {draft.mode}
            </span>
            <span className="text-xs font-mono text-zinc-600">{draft.pattern}</span>
          </>
        ) : (
          <span className="text-xs text-zinc-400">No check</span>
        )
      )}
    </div>
  );
}

function ToolRow({
  tool,
  config,
  upstreamId,
  onToolChange,
  forcedValue,
  constraints,
  onConstraintsSave,
  onUnsavedChange,
}: {
  tool: ToolInfo;
  config: ToolAccessConfig | undefined;
  upstreamId: string;
  onToolChange: (upstreamId: string, toolName: string, value: boolean) => void;
  forcedValue?: boolean;
  constraints: Record<string, ArgumentConstraint>;
  onConstraintsSave: (upstreamId: string, toolName: string, drafts: Record<string, DraftConstraint>) => Promise<boolean>;
  onUnsavedChange: (toolName: string, dirty: boolean) => void;
}) {
  const { confirm, dialogProps } = useConfirm();
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const flags = toolFlags(tool);
  const disabled = forcedValue !== undefined;
  const hasExplicit = config?.tools
    ? tool.original_name in config.tools
    : false;
  const defaultValue = resolveToolDefault(config, flags);
  const value = disabled ? forcedValue : hasExplicit ? config!.tools[tool.original_name] : defaultValue;

  const properties = (tool.input_schema?.properties ?? {}) as Record<string, Record<string, unknown>>;
  const argNames = Object.keys(properties);
  const hasArgs = argNames.length > 0;
  const hasConstraints = Object.keys(constraints).length > 0;

  // Draft state for editing — initialized from saved constraints
  const makeDrafts = () => Object.fromEntries(
    argNames.map((name) => {
      const c = constraints[name];
      return [name, { mode: c?.mode ?? "", pattern: c?.pattern ?? "" } as DraftConstraint];
    })
  );
  const [drafts, setDrafts] = useState<Record<string, DraftConstraint>>(makeDrafts);

  // Sync drafts when saved constraints change (e.g. after save)
  useEffect(() => {
    if (!editing) setDrafts(makeDrafts());
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [constraints]);

  const isDirty = () => {
    return argNames.some((name) => {
      const saved = constraints[name];
      const draft = drafts[name];
      if (!saved && !draft?.mode) return false;
      if (!saved && draft?.mode) return true;
      if (saved && !draft?.mode) return true;
      return saved.pattern !== draft.pattern || saved.mode !== draft.mode;
    });
  };

  const validateConstraint = (d: DraftConstraint): string => {
    if (!d.mode) return "";
    if (!d.pattern.trim()) return "empty";
    try { new RegExp(d.pattern); return ""; } catch { return "Invalid regex"; }
  };

  const handleEditDraft = (argName: string, d: DraftConstraint) => {
    const next = { ...drafts, [argName]: d };
    setDrafts(next);
    setErrors((e) => ({ ...e, [argName]: validateConstraint(d) }));
  };

  const enterEdit = () => {
    setDrafts(makeDrafts());
    setEditing(true);
    setErrors({});
    onUnsavedChange(tool.original_name, false);
  };

  const cancelEdit = () => {
    setDrafts(makeDrafts());
    setEditing(false);
    setErrors({});
    onUnsavedChange(tool.original_name, false);
  };

  const hasErrors = Object.values(errors).some((e) => e !== "");

  const handleSave = async () => {
    if (hasErrors) return;
    const ok = await onConstraintsSave(upstreamId, tool.original_name, drafts);
    if (ok) {
      setEditing(false);
      setErrors({});
      onUnsavedChange(tool.original_name, false);
    }
  };

  // Track dirty state for parent
  useEffect(() => {
    if (editing) onUnsavedChange(tool.original_name, isDirty());
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drafts, editing]);

  const tryCollapse = async () => {
    if (editing && isDirty()) {
      const ok = await confirm({
        title: "Unsaved changes",
        message: "You have unsaved argument check changes. Discard them?",
        confirmLabel: "Discard",
        cancelLabel: "Keep editing",
        destructive: true,
      });
      if (!ok) return;
      cancelEdit();
    }
    setExpanded(false);
    setEditing(false);
    onUnsavedChange(tool.original_name, false);
  };

  return (
    <>
      <tr className="border-t border-zinc-100 hover:bg-zinc-50">
        <td className="px-4 py-2">
          <div className="flex items-center gap-1.5">
            {hasArgs ? (
              <button
                onClick={() => expanded ? tryCollapse() : setExpanded(true)}
                className="text-zinc-400 hover:text-zinc-600 shrink-0"
              >
                {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </button>
            ) : (
              <span className="w-[14px] shrink-0" />
            )}
            <span className={disabled ? "text-zinc-400" : "text-zinc-700"}>{tool.original_name}</span>
            {hasConstraints && (
              <span className="px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded text-[10px] font-medium">
                check
              </span>
            )}
          </div>
          {tool.annotations && <div className="pl-[22px]"><ToolAnnotationBadges annotations={tool.annotations} /></div>}
          {tool.description && (
            <p className="text-xs text-zinc-400 mt-0.5 truncate max-w-lg pl-[22px]">
              {tool.description}
            </p>
          )}
        </td>
        <td className="px-4 py-2 text-right">
          <div className="inline-block">
            <SettingToggle
              value={value}
              onChange={(v) => onToolChange(upstreamId, tool.original_name, v)}
              disabled={disabled}
            />
          </div>
        </td>
      </tr>
      {expanded && hasArgs && (
        <tr className="bg-zinc-50/50">
          <td colSpan={2} className="px-4 py-2 pl-[38px]">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs text-zinc-500 font-medium">Argument checks</span>
              <div className="flex items-center gap-1.5">
                {editing ? (
                  <>
                    <button
                      onClick={handleSave}
                      disabled={hasErrors}
                      className="px-2 py-1 text-xs bg-blue-600 text-white rounded not-disabled:hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      Save
                    </button>
                    <button
                      onClick={cancelEdit}
                      className="px-2 py-1 text-xs bg-zinc-100 text-zinc-700 rounded hover:bg-zinc-200"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={enterEdit}
                    className="px-2 py-1 text-xs text-blue-600 hover:bg-blue-50 rounded flex items-center gap-1"
                  >
                    <Pencil size={10} /> Edit
                  </button>
                )}
              </div>
            </div>
            {editing && (
              <p className="text-[11px] text-zinc-400 mb-1">
                Uses{" "}
                <a
                  href="https://docs.python.org/3/howto/regex.html"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline hover:text-zinc-600"
                >
                  Python regex syntax
                </a>
              </p>
            )}
            {argNames.map((argName) => {
              const argSchema = properties[argName];
              const argType = (argSchema?.type as string) ?? "";
              return (
                <ConstraintRow
                  key={argName}
                  argName={argName}
                  argType={argType}
                  draft={drafts[argName] ?? { mode: "", pattern: "" }}
                  editing={editing}
                  error={errors[argName] ?? ""}
                  onDraftChange={(d) => handleEditDraft(argName, d)}
                />
              );
            })}
          </td>
        </tr>
      )}
      <ConfirmDialog {...dialogProps} />
    </>
  );
}

function McpToolDrillDown({
  role,
  upstreamId,
  upstream,
  tools,
  onBack,
  onToolChange,
  onDefaultEnabledChange,
  onAnnotationDefaultChange,
  onConstraintsSave,
  onUnsavedChange,
}: {
  role: RoleAccessInfo;
  upstreamId: string;
  upstream: UpstreamSummary | undefined;
  tools: ToolInfo[];
  onBack: () => void;
  onToolChange: (upstreamId: string, toolName: string, value: boolean) => void;
  onDefaultEnabledChange: (upstreamId: string, value: boolean | null) => void;
  onAnnotationDefaultChange: (upstreamId: string, annotation: string, value: boolean | null) => void;
  onConstraintsSave: (upstreamId: string, toolName: string, drafts: Record<string, DraftConstraint>) => Promise<boolean>;
  onUnsavedChange: (toolName: string, dirty: boolean) => void;
}) {
  const config = role.tool_access?.[upstreamId];
  const { categories, uncategorized } = categorizeTools(tools);

  if (!upstream) return <p className="text-red-500">MCP not found</p>;

  return (
    <>
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-700 mb-4"
      >
        <ArrowLeft size={14} />
        Back to MCP Permissions
      </button>

      <h3 className="text-lg font-medium text-zinc-900 mb-3">
        {upstream.display_name} — Tool Permissions
      </h3>

      {tools.length === 0 ? (
        <p className="text-sm text-zinc-500">No tools discovered. The upstream MCP may need to be connected first.</p>
      ) : (
      <div className="space-y-4">
        {/* Fixed category sections — always shown */}
        {CATEGORY_PRIORITY.map((catKey) => {
          const cat = categories.find((c) => c.key === catKey);
          const catTools = cat?.tools ?? [];
          const catMode: boolean | null = config?.category_defaults?.[catKey] ?? null;
          return (
            <div key={catKey}>
              <div className="flex items-center justify-between mb-2 pr-4">
                <h4 className="text-sm font-medium text-zinc-700">
                  {ANNOTATION_LABELS[catKey]} tools
                  <span className="text-zinc-400 font-normal ml-1.5">({catTools.length})</span>
                </h4>
                <CategoryToggle
                  value={catMode}
                  onChange={(v) => onAnnotationDefaultChange(upstreamId, catKey, v)}
                />
              </div>
              {catTools.length > 0 && (
                <div className="border border-zinc-200 rounded-lg overflow-hidden">
                  <table className="w-full text-sm">
                    <thead className="bg-zinc-50 text-zinc-600">
                      <tr>
                        <th className="text-left px-4 py-2"></th>
                        <th className="text-right px-4 py-2 font-medium">Enabled</th>
                      </tr>
                    </thead>
                    <tbody>
                      {catTools.map((tool) => (
                        <ToolRow
                          key={tool.original_name}
                          tool={tool}
                          config={config}
                          upstreamId={upstreamId}
                          onToolChange={onToolChange}
                          forcedValue={catMode !== null ? catMode : undefined}
                          constraints={role.argument_constraints?.[`${upstreamId}__${tool.original_name}`] ?? {}}
                          onConstraintsSave={onConstraintsSave}
                          onUnsavedChange={onUnsavedChange}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}

        {/* Uncategorized tools */}
        {uncategorized.length > 0 && (() => {
          const otherMode: boolean | null = config?.fallback_enabled ?? null;
          return (
            <div>
              <div className="flex items-center justify-between mb-2 pr-4">
                <h4 className="text-sm font-medium text-zinc-700">
                  Other tools
                  <span className="text-zinc-400 font-normal ml-1.5">({uncategorized.length})</span>
                </h4>
                <CategoryToggle
                  value={otherMode}
                  onChange={(v) => onDefaultEnabledChange(upstreamId, v)}
                />
              </div>
              <div className="border border-zinc-200 rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-50 text-zinc-600">
                    <tr>
                      <th className="text-left px-4 py-2"></th>
                      <th className="text-right px-4 py-2 font-medium">Enabled</th>
                    </tr>
                  </thead>
                  <tbody>
                    {uncategorized.map((tool) => (
                      <ToolRow
                        key={tool.original_name}
                        tool={tool}
                        config={config}
                        upstreamId={upstreamId}
                        onToolChange={onToolChange}
                        forcedValue={otherMode !== null ? otherMode : undefined}
                        constraints={role.argument_constraints?.[`${upstreamId}__${tool.original_name}`] ?? {}}
                        onConstraintsSave={onConstraintsSave}
                        onUnsavedChange={onUnsavedChange}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })()}
      </div>
      )}
    </>
  );
}

export function AccessPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const isFreePlan = (user?.current_org?.plan ?? "free") === "free";
  const [roles, setRoles] = useState<RoleAccessInfo[]>([]);
  const [roleSummaries, setRoleSummaries] = useState<RoleSummary[]>([]);
  const [upstreams, setUpstreams] = useState<UpstreamSummary[]>([]);
  const [allTools, setAllTools] = useState<ToolInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchParams, setSearchParams] = useSearchParams();
  const activeRole = searchParams.get("role") ?? "";
  const setActiveRole = (role: string) => {
    setSearchParams(role ? { role } : {}, { replace: true });
  };
  const [creatingRole, setCreatingRole] = useState(false);
  const [newRoleName, setNewRoleName] = useState("");
  const [newRoleCopyFrom, setNewRoleCopyFrom] = useState("");
  const [createError, setCreateError] = useState("");
  const [editingRoleName, setEditingRoleName] = useState<string | null>(null);
  const [selectedMcp, setSelectedMcp] = useState<string | null>(null);
  const [roleOrder, setRoleOrder] = useState<string[]>(() => {
    try {
      const stored = localStorage.getItem("mcpolis:role-tab-order");
      return stored ? JSON.parse(stored) : [];
    } catch { return []; }
  });
  const [draggedRole, setDraggedRole] = useState<string | null>(null);
  const newRoleInputRef = useRef<HTMLInputElement>(null);

  const reload = () => {
    Promise.all([fetchRoleAccess(), fetchUpstreams(), fetchRoles(), fetchAllTools()])
      .then(([r, u, rs, tools]) => {
        setRoles(r);
        setUpstreams(u);
        setRoleSummaries(rs);
        setAllTools(tools);
      })
      // Don't let a failed load (e.g. a 401 after the session died)
      // escape as an unhandled rejection; the global session-expiry
      // handler in apiFetch routes 401s to the sign-in screen.
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  useEffect(() => {
    if (creatingRole && newRoleInputRef.current) {
      newRoleInputRef.current.focus();
    }
  }, [creatingRole]);

  const handleChange = async (roleName: string, mcpId: string, value: boolean) => {
    await setRoleMcpAccessEntry(roleName, mcpId, value);
    reload();
  };

  const handleDefaultEnabledChange = async (roleName: string, value: boolean) => {
    await setRoleAutoEnableNew(roleName, value);
    reload();
  };

  const handleToolChange = async (upstreamId: string, toolName: string, value: boolean) => {
    if (value === resolveToolDefault(roles.find(r => r.name === effectiveActiveRole)?.tool_access?.[upstreamId], toolFlags(allTools.find(t => t.upstream_id === upstreamId && t.original_name === toolName)!))) {
      // If setting back to default, remove the override
      await removeRoleToolAccessEntry(effectiveActiveRole, upstreamId, toolName);
    } else {
      await setRoleToolAccessEntry(effectiveActiveRole, upstreamId, toolName, value);
    }
    reload();
  };

  const handleToolDefaultEnabledChange = async (upstreamId: string, value: boolean | null) => {
    await setRoleToolFallbackEnabled(effectiveActiveRole, upstreamId, value);
    reload();
  };

  const handleAnnotationDefaultChange = async (upstreamId: string, annotation: string, value: boolean | null) => {
    if (value === null) {
      await removeRoleCategoryDefault(effectiveActiveRole, upstreamId, annotation);
    } else {
      await setRoleCategoryDefault(effectiveActiveRole, upstreamId, annotation, value);
    }
    reload();
  };

  const handleConstraintsSave = async (upstreamId: string, toolName: string, drafts: Record<string, DraftConstraint>): Promise<boolean> => {
    try {
      const saved = roles.find(r => r.name === effectiveActiveRole)?.argument_constraints?.[`${upstreamId}__${toolName}`] ?? {};
      for (const [argName, draft] of Object.entries(drafts)) {
        const prev = saved[argName];
        if (draft.mode && draft.pattern.trim()) {
          if (!prev || prev.pattern !== draft.pattern || prev.mode !== draft.mode) {
            await setRoleArgumentConstraint(effectiveActiveRole, upstreamId, toolName, argName, draft.pattern, draft.mode as "allow" | "forbid");
          }
        } else if (!draft.mode && prev) {
          await removeRoleArgumentConstraint(effectiveActiveRole, upstreamId, toolName, argName);
        }
      }
      reload();
      return true;
    } catch (e) {
      if (maybeHandlePlanLimit(e, { source: "argument_constraint_save" })) {
        return false;
      }
      return false;
    }
  };

  const [dirtyTools, setDirtyTools] = useState<Set<string>>(new Set());
  const handleUnsavedChange = (toolName: string, dirty: boolean) => {
    setDirtyTools(prev => {
      const next = new Set(prev);
      if (dirty) next.add(toolName); else next.delete(toolName);
      return next;
    });
  };
  const hasUnsavedConstraints = dirtyTools.size > 0;

  const { confirm: confirmNav, dialogProps: navDialogProps } = useConfirm();
  const guardedNavigate = async (action: () => void) => {
    if (hasUnsavedConstraints) {
      const ok = await confirmNav({
        title: "Unsaved changes",
        message: "You have unsaved argument check changes. Discard them?",
        confirmLabel: "Discard",
        cancelLabel: "Keep editing",
        destructive: true,
      });
      if (!ok) return;
      setDirtyTools(new Set());
    }
    action();
  };

  const handleCreateRole = async () => {
    const name = newRoleName.toLowerCase().replace(/[^a-z0-9\-_.]/g, "");
    if (!name) return;
    try {
      setCreateError("");
      await createRole(name, newRoleCopyFrom || undefined);
      setCreatingRole(false);
      setNewRoleName("");
      setNewRoleCopyFrom("");
      reload();
      setActiveRole(name);
    } catch (e) {
      if (maybeHandlePlanLimit(e, { source: "add_role_button" })) {
        setCreatingRole(false);
        return;
      }
      setCreateError(e instanceof Error ? e.message : "Failed to create role");
    }
  };

  const handleCancelCreate = () => {
    setCreatingRole(false);
    setNewRoleName("");
    setNewRoleCopyFrom("");
    setCreateError("");
  };

  const handleRename = async (oldName: string, newName: string) => {
    await renameRole(oldName, newName);
    reload();
    setActiveRole(newName);
  };

  const handleDeleteRole = async (name: string) => {
    await deleteRole(name);
    // Switch to first remaining role
    const remaining = roles.filter((r) => r.name !== name);
    setActiveRole(remaining[0]?.name ?? "");
    reload();
  };

  // Sort roles by stored order, then alphabetically for new ones
  const sortedRoles = [...roles].sort((a, b) => {
    const ai = roleOrder.indexOf(a.name);
    const bi = roleOrder.indexOf(b.name);
    if (ai !== -1 && bi !== -1) return ai - bi;
    if (ai !== -1) return -1;
    if (bi !== -1) return 1;
    return a.name.localeCompare(b.name);
  });

  const saveRoleOrder = (names: string[]) => {
    setRoleOrder(names);
    localStorage.setItem("mcpolis:role-tab-order", JSON.stringify(names));
  };

  const handleDragStart = (roleName: string) => {
    setDraggedRole(roleName);
  };

  const handleDragOver = (e: React.DragEvent, targetName: string) => {
    if (!draggedRole || targetName === draggedRole) return;
    e.preventDefault();
  };

  const handleDrop = (targetName: string) => {
    if (!draggedRole || targetName === draggedRole) return;
    const names = sortedRoles.map((r) => r.name);
    const fromIdx = names.indexOf(draggedRole);
    const toIdx = names.indexOf(targetName);
    if (fromIdx === -1 || toIdx === -1) return;
    names.splice(fromIdx, 1);
    names.splice(toIdx, 0, draggedRole);
    saveRoleOrder(names);
    setDraggedRole(null);
  };

  const effectiveActiveRole = activeRole || sortedRoles[0]?.name || "";
  const activeRoleData = sortedRoles.find((r) => r.name === effectiveActiveRole);

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div>
      <h2 className="text-xl font-semibold text-zinc-900 mb-1">
        {t("access.title")}
      </h2>
      <p className="text-sm text-zinc-500 mb-6">
        {t("access.description")}
      </p>

      {/* Role tabs */}
      <div className="flex gap-1 border-b border-zinc-200 mb-6">
        {sortedRoles.map((role) => (
          <button
            key={role.name}
            onClick={() => guardedNavigate(() => { setActiveRole(role.name); handleCancelCreate(); })}
            draggable
            onDragStart={() => handleDragStart(role.name)}
            onDragOver={(e) => handleDragOver(e, role.name)}
            onDrop={() => handleDrop(role.name)}
            onDragEnd={() => setDraggedRole(null)}
            className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors ${
              effectiveActiveRole === role.name && !creatingRole
                ? "border-zinc-900 text-zinc-900"
                : "border-transparent text-zinc-400 hover:text-zinc-600"
            } cursor-grab active:cursor-grabbing ${
              draggedRole === role.name ? "opacity-50" : ""
            }`}
          >
            {effectiveActiveRole === role.name && editingRoleName !== null
              ? (editingRoleName || <span className="text-zinc-300 italic">role-name</span>)
              : role.name}
            {role.is_admin && role.name.toLowerCase() !== t("auth.adminBadge").toLowerCase() && (
              <span className="ml-1.5 px-1 py-0.5 bg-purple-100 text-purple-700 rounded text-[10px]">
                {t("auth.adminBadge").toLowerCase()}
              </span>
            )}
          </button>
        ))}

        {/* Create role tab */}
        {creatingRole ? (
          <span className="px-4 py-2 text-sm font-medium -mb-px border-b-2 border-zinc-900 text-zinc-900">
            {newRoleName || <span className="text-zinc-300 italic">new role</span>}
          </span>
        ) : (
          <button
            onClick={() => {
              if (isFreePlan) {
                // Pre-flight: Free has ``max_custom_roles=0`` so the
                // create call would 402. Surface the upgrade dialog
                // up-front rather than letting the user start typing
                // a name only to get gated at submit.
                handlePlanLimitError(
                  new PlanLimitError(
                    "Custom roles aren't available on Free.",
                    "max_custom_roles",
                    0,
                    0,
                  ),
                  { source: "add_role_plus_button" },
                );
                return;
              }
              setCreatingRole(true);
            }}
            className="px-2 py-2 text-zinc-400 hover:text-zinc-700 -mb-px border-b-2 border-transparent"
            title="Add role"
          >
            <Plus size={14} />
          </button>
        )}
      </div>

      {/* Create role edit card */}
      {creatingRole && (
        <div className="mb-6">
          <div className="flex items-center gap-2 text-sm mb-2">
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={handleCreateRole}
                disabled={!newRoleName || roles.some((r) => r.name === newRoleName)}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Create
              </button>
              <button
                onClick={handleCancelCreate}
                className="px-3 py-1.5 bg-zinc-100 text-zinc-700 text-sm rounded hover:bg-zinc-200"
              >
                {t("common.cancel")}
              </button>
            </div>
          </div>
          <div className="bg-blue-50/50 border border-blue-100 rounded-lg p-4 text-sm space-y-3">
            <div className="flex items-end gap-4">
              <div>
                <label className="block text-xs font-medium text-zinc-500 mb-1">Name</label>
                <IdInput
                  inputRef={newRoleInputRef}
                  value={newRoleName}
                  onChange={setNewRoleName}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreateRole();
                    if (e.key === "Escape") handleCancelCreate();
                  }}
                  placeholder="role-name"
                  className="w-48"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-zinc-500 mb-1">Copy settings from</label>
                <select
                  value={newRoleCopyFrom}
                  onChange={(e) => setNewRoleCopyFrom(e.target.value)}
                  className="px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
                >
                  <option value="">None (empty role)</option>
                  {sortedRoles.map((r) => (
                    <option key={r.name} value={r.name}>
                      {r.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            {roles.some((r) => r.name === newRoleName) && (
              <p className="text-xs text-red-500">A role with this name already exists</p>
            )}
            {createError && (
              <p className="text-xs text-red-500">{createError}</p>
            )}
          </div>
        </div>
      )}

      {/* Role settings (non-creating, not drilling down) */}
      {!creatingRole && activeRoleData && !selectedMcp && (
        <RoleSettings
          role={activeRoleData}
          allRoles={roles}
          roleSummaries={roleSummaries}
          onRename={handleRename}
          onDelete={handleDeleteRole}
          onEditNameChange={setEditingRoleName}
          planBlocksDelete={isFreePlan}
        />
      )}

      {/* Active role content */}
      {!creatingRole && activeRoleData && !selectedMcp && (
        <RoleSection
          role={activeRoleData}
          upstreams={upstreams}
          allTools={allTools}
          onChange={handleChange}
          onDefaultEnabledChange={handleDefaultEnabledChange}
          onToolsClick={setSelectedMcp}
        />
      )}

      {/* Tool access drill-down for selected MCP */}
      {!creatingRole && activeRoleData && selectedMcp && (
        <McpToolDrillDown
          role={activeRoleData}
          upstreamId={selectedMcp}
          upstream={upstreams.find((u) => u.id === selectedMcp)}
          tools={allTools.filter((t) => t.upstream_id === selectedMcp)}
          onBack={() => guardedNavigate(() => setSelectedMcp(null))}
          onToolChange={handleToolChange}
          onDefaultEnabledChange={handleToolDefaultEnabledChange}
          onAnnotationDefaultChange={handleAnnotationDefaultChange}
          onConstraintsSave={handleConstraintsSave}
          onUnsavedChange={handleUnsavedChange}
        />
      )}
      <ConfirmDialog {...navDialogProps} />
    </div>
  );
}
