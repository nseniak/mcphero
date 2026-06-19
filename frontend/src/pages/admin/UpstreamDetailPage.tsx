import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link, useNavigate } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../../hooks/useAuth";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import { fetchUpstream, fetchUpstreamTools, refreshUpstreamTools, updateUpstream, removeUpstream } from "../../api/admin";
import { ApiError, withOrgParam } from "../../api/client";
import { fetchSandboxCapabilities } from "../../api/sandbox";
import { SandboxComboSelect } from "../../components/SandboxComboSelect";
import { UpstreamActionButtons } from "../../components/UpstreamActionButtons";
import type {
  SandboxCapabilitiesResponse,
  SandboxResourcesView,
  ToolInfo,
  UpstreamDetail,
} from "../../api/types";
import { ToolRow } from "../../components/ToolTable";
import { useTranslation, type TranslationKey } from "../../i18n/index";
import { en } from "../../i18n/en";
import { StatusBadge } from "../../components/StatusBadge";
import { upstreamStatusLabel } from "../../lib/upstreamStatusLabel";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { useEventSource } from "../../hooks/useEventSource";
import { ArrowLeft, ChevronDown, ChevronRight, Loader2, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { ActionButton } from "../../components/ui/action-button";
import { SettingsCard, SettingsField } from "../../components/SettingsCard";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import { FieldHint } from "../../components/ui/field-hint";
import { TransportBadge } from "../../components/TransportBadge";
import { JsonConfigEditor } from "../../components/JsonConfigEditor";
import {
  TemplateVarsManager,
  EMPTY_PENDING_CHANGES,
  type TemplateVarsManagerHandle,
  type PendingTemplateVarChanges,
} from "../../components/TemplateVarsManager";
import { SandboxFilesManager } from "../../components/SandboxFilesManager";
import { DirtyConfigBanner } from "../../components/DirtyConfigBanner";
import {
  SecretDetectionDialog,
  clearSecretDetectionDismissed,
  isSecretDetectionDismissed,
  type DetectionResolution,
} from "../../components/SecretDetectionDialog";
import {
  scanConfigForSecrets,
  type ScanFinding,
} from "../../lib/secret-detection";
import {
  findReferences,
  findReferencesInSandboxFiles,
  type TemplateVarReference,
} from "../../lib/template-var-references";
import { listTemplateVars } from "../../api/template-vars";
import { listSandboxFiles } from "../../api/sandbox-files";
import type { SandboxFileSummary } from "../../api/types";
import type { TemplateVarSummary } from "../../api/types";

const AUTH_MODES = ["admin_oauth", "per_user_oauth", "service_account"] as const;

// Floor on the refresh spinner. A sub-second flicker reads as "broken"
// rather than "did something", so hold the spin for at least this long.
const MIN_SPIN_MS = 2000;

// ToolRow, ToolAnnotationBadges, ToolParamList, ToolTable are in ../../components/ToolTable

function LogViewer({ upstreamId, resetEpoch }: {
  upstreamId: string;
  /**
   * Bumped synchronously by the parent on every Start / Reconnect
   * click. Used as a useEffect dep so the local SSE accumulator
   * resets the same instant the operator clicks Start — instead
   * of waiting for ``upstream.starting`` to flip true via reload
   * (which can race a fast-failing command and never observe the
   * intermediate state, leaving the OLD stderr on screen).
   */
  resetEpoch: number;
}) {
  const { t } = useTranslation();
  const [logs, setLogs] = useState("");
  const [open, setOpen] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    setLogs("");
  }, [resetEpoch]);

  // EventSource is gated on ``open``: we tear it down when the
  // panel collapses and re-open on expand. The backend's SSE
  // handler creates the LogBuffer eagerly via ``get_or_create``
  // so the handshake returns 200 even before the MCP has ever
  // started; the subscriber simply sits idle until stderr arrives.
  // ``subscribe()`` first-yields the current ring contents on
  // every fresh connect, so the operator sees backlog on re-expand.
  useEffect(() => {
    if (!open) return;
    setLogs("");
    const es = new EventSource(withOrgParam(`/api/admin/upstreams/${upstreamId}/logs/stream`));
    es.onmessage = (event) => {
      const chunk = JSON.parse(event.data) as string;
      setLogs((prev) => prev + chunk);
    };
    es.onerror = () => {
      // EventSource auto-reconnect handles transient drops.
      // Fatal closes (browser opted out after a non-200) are rare
      // now that the handler always returns 200 for stdio.
    };
    return () => {
      es.close();
    };
  }, [open, upstreamId]);

  // Auto-scroll to bottom when new logs arrive.
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [logs]);

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-zinc-500 hover:text-zinc-700"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        Execution logs
      </button>
      {open && (
        <div className="mt-1.5">
          {logs ? (
            <pre
              ref={preRef}
              className="p-3 bg-zinc-50 text-zinc-700 rounded text-xs font-mono overflow-x-auto max-h-80 overflow-y-auto"
            >
              {logs}
            </pre>
          ) : (
            <p className="text-xs text-zinc-400">{t("upstreamDetail.noLogs")}</p>
          )}
        </div>
      )}
    </div>
  );
}

export function UpstreamDetailPage() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const orgSlug = useOrgSlug();
  const { user } = useAuth();
  const { confirm, dialogProps } = useConfirm();
  const queryClient = useQueryClient();
  const [upstream, setUpstream] = useState<UpstreamDetail | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [refreshingTools, setRefreshingTools] = useState(false);
  const [loading, setLoading] = useState(true);
  const [configOpen, setConfigOpen] = useState(false);
  // Bumped synchronously by ``handleOptimisticReset`` on every Start
  // / Reconnect click. Drives the LogViewer's local-accumulator
  // reset immediately, ahead of the API roundtrip.
  const [logsResetEpoch, setLogsResetEpoch] = useState(0);
  const [editing, setEditing] = useState(false);

  // Editable field state
  const [editName, setEditName] = useState("");
  const [editAuthMode, setEditAuthMode] = useState("service_account");
  const [editClientId, setEditClientId] = useState("");
  const [editClientSecret, setEditClientSecret] = useState("");
  const [editConfig, setEditConfig] = useState("");
  const [saving, setSaving] = useState(false);

  // Resource picker state — relocated here from the deleted Sandbox
  // tab. ``capabilities`` is fetched once per page load (the active
  // provider's CPU/RAM/disk grid + persistent-disk support flag);
  // ``resources`` is the per-MCP saved values, mirrored locally so
  // the form can be edited without thrashing the upstream object.
  const [capabilities, setCapabilities] =
    useState<SandboxCapabilitiesResponse | null>(null);
  const [resources, setResources] = useState<SandboxResourcesView | null>(
    null,
  );
  // Resource validation feedback. Populated by the SETTINGS Save
  // flow when the unified PUT rejects with a structured 400
  // ``{message, field, value}``. ``resourcesField`` flags the
  // offending control (CPU dropdown / RAM dropdown / disk dropdown);
  // ``resourcesError`` carries the human-readable detail.
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [resourcesField, setResourcesField] = useState<string | null>(null);

  // Per-MCP env vars (read-mostly: server list reloaded on mount and
  // after every SETTINGS Save). Edit-mode mutations live in
  // ``pendingTemplateVarChanges`` and only commit on Save.
  const [secrets, setSecrets] = useState<TemplateVarSummary[]>([]);
  const reloadSecrets = async () => {
    if (!id) return;
    try {
      setSecrets(await listTemplateVars(id));
    } catch {
      // best-effort; surfaced via the TemplateVarsManager's own error path
    }
  };
  // Sandbox-file summaries — fetched at the page level so the
  // Variables card can scan target_paths for ``${...}`` references
  // and surface unresolved tokens in its amber callout, the same
  // way it scans the JSON config. Refreshed alongside Variables on
  // every save so a freshly-uploaded file's path immediately shows
  // up as a reference source.
  const [sandboxFiles, setSandboxFiles] = useState<SandboxFileSummary[]>([]);
  const reloadSandboxFiles = async () => {
    if (!id) return;
    try {
      setSandboxFiles(await listSandboxFiles(id));
    } catch {
      // best-effort; the SandboxFilesManager has its own error path
    }
  };
  useEffect(() => {
    void reloadSecrets();
    void reloadSandboxFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  const templateVarsManagerRef = useRef<TemplateVarsManagerHandle | null>(null);

  // Deferred env-var mutation buffer. Populated by ``TemplateVarsManager``
  // in edit mode; flushed via the ``template_var_changes`` field on the
  // SETTINGS Save PUT; cleared on Cancel.
  const [pendingTemplateVarChanges, setPendingTemplateVarChanges] =
    useState<PendingTemplateVarChanges>(EMPTY_PENDING_CHANGES);
  // Names visible in the buffer (server names + buffer-adds, minus
  // buffer-deletes) — drives the duplicate-name guard in
  // ``TemplateVarsManager`` and the existing-name awareness in
  // ``SecretDetectionDialog``.
  const effectiveTemplateVarNames: Set<string> = (() => {
    const names = new Set<string>(secrets.map((s) => s.name));
    for (const n of pendingTemplateVarChanges.deletes) names.delete(n);
    for (const n of Object.keys(pendingTemplateVarChanges.sets)) names.add(n);
    return names;
  })();

  // Pre-save secret-detection state.
  const [pendingFindings, setPendingFindings] = useState<ScanFinding[]>([]);
  // Per-MCP "don't show again" flag, mirrored into React state so the
  // undo link can react to clicks without reloading the page. The
  // localStorage key is the source of truth — this state is just a
  // re-render trigger.
  const [detectionDismissed, setDetectionDismissed] = useState<boolean>(
    () => (id ? isSecretDetectionDismissed(id) : false),
  );
  useEffect(() => {
    setDetectionDismissed(id ? isSecretDetectionDismissed(id) : false);
  }, [id]);
  const showDetectionAgain = useCallback(() => {
    if (!id) return;
    clearSecretDetectionDismissed(id);
    setDetectionDismissed(false);
  }, [id]);

  const resetEditState = (u: UpstreamDetail) => {
    setEditName(u.display_name);
    setEditAuthMode(u.auth_mode);
    setEditClientId(u.client_id ?? "");
    setEditClientSecret("");
    setEditConfig(JSON.stringify(u.server_config, null, 2));
  };

  const reload = () => {
    if (!id) return;
    Promise.all([fetchUpstream(id), fetchUpstreamTools(id)])
      .then(([u, tl]) => {
        setUpstream(u);
        setTools(tl);
        resetEditState(u);
      })
      .catch(() => {
        // Swallow so a failed load never escapes as an unhandled
        // promise rejection. On a 401 (dead session) the global
        // session-expiry handler in apiFetch already routes to the
        // sign-in screen; for any other error the page falls back to
        // its "not found" render once ``loading`` clears below.
      })
      .finally(() => setLoading(false));
    queryClient.invalidateQueries({ queryKey: ["upstreams"] });
  };

  useEffect(reload, [id]);

  // Re-pull the live session's tool list on demand. The button is
  // gated on the upstream being active (below), and the backend
  // refuses (409) rather than connecting. The refresh is NON-BLOCKING:
  // the POST returns immediately ("started") and the acquire+refresh
  // runs in the background, so an E2B-pause stall can't time out the
  // click (2026-06-18 incident). Completion — success OR a recorded
  // error — arrives via ``policy_changed`` -> ``reload``, which repaints
  // the fresh tool list or the red status + error banner.
  const handleRefreshTools = async () => {
    if (!id) return;
    setRefreshingTools(true);
    const started = Date.now();
    let failureMessage: string | null = null;
    try {
      const res = await refreshUpstreamTools(id);
      // Non-blocking refresh returns connected=true ("started"); the
      // outcome lands later via policy_changed -> reload (badge/banner).
      // This connected=false branch is a defensive fallback; a non-2xx
      // (e.g. 409 not-active) still throws below into the popup path.
      if (!res.connected) {
        failureMessage = res.error ?? t("upstreamDetail.refreshFailed");
      }
    } catch (e) {
      failureMessage = e instanceof Error ? e.message : String(e);
    }
    const elapsed = Date.now() - started;
    if (elapsed < MIN_SPIN_MS) {
      await new Promise((r) => setTimeout(r, MIN_SPIN_MS - elapsed));
    }
    // reload() repaints the tool list (success) or the red status +
    // disconnect_reason banner (failure) from server truth.
    reload();
    setRefreshingTools(false);
    if (failureMessage !== null) {
      // Fire-and-forget alert: a single-button popup so the failure
      // is unmissable, on top of the status badge flip.
      void confirm({
        title: t("upstreamDetail.refreshFailed"),
        message: failureMessage,
        confirmLabel: t("common.close"),
        cancelLabel: "",
        destructive: true,
      });
    }
  };

  // Fetch the active provider's capabilities once per page load.
  // The grid drives the CPU/RAM dropdown options + whether the
  // disk control / persistent-volume toggle are renderable.
  useEffect(() => {
    fetchSandboxCapabilities()
      .then(setCapabilities)
      .catch((e: unknown) => {
        setResourcesError(e instanceof Error ? e.message : String(e));
      });
  }, []);

  // Mirror the upstream's saved resource view into local form state
  // each time the upstream is (re)loaded. The form edits ``resources``
  // and the SETTINGS Save button flushes any change as part of the
  // unified ``updateUpstream`` PUT (``sandbox_resources`` patch).
  useEffect(() => {
    if (upstream?.sandbox_resources) {
      setResources(upstream.sandbox_resources);
    }
  }, [upstream]);

  // policy_changed fires when readiness flips (an admin in another
  // tab connects, the post-connect catalog refresh finishes, a
  // token refresh succeeds, …). Full refetch so we pick up the new
  // ready / slot_owner / starting / connected_users at once.
  useEventSource({
    policy_changed: () => {
      reload();
    },
  });

  const enterEdit = () => {
    if (upstream) resetEditState(upstream);
    setPendingTemplateVarChanges(EMPTY_PENDING_CHANGES);
    setEditing(true);
  };

  const cancelEdit = () => {
    if (upstream) resetEditState(upstream);
    setPendingTemplateVarChanges(EMPTY_PENDING_CHANGES);
    setEditing(false);
  };

  const handleRemove = async () => {
    if (!id) return;
    const ok = await confirm({
      title: t("common.remove"),
      message: t("upstreams.confirmRemove", { id: upstream?.display_name ?? id }),
      confirmLabel: t("common.remove"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    await removeUpstream(id);
    navigate(`/orgs/${orgSlug}/admin/upstream`);
  };


  // Has the user changed cosmetic fields?
  const cosmeticDirty =
    upstream !== null &&
    editName !== upstream.display_name;

  // Has the user changed auth mode?
  const authModeDirty =
    upstream !== null && editAuthMode !== upstream.auth_mode;

  // Has the user changed OAuth client credentials?
  const clientIdDirty =
    upstream !== null && editClientId !== (upstream.client_id ?? "");
  const clientSecretDirty = editClientSecret !== "";
  const oauthCredsDirty = clientIdDirty || clientSecretDirty;

  // Has the user changed the JSON config? (compare parsed data, not formatting)
  const configDirty = (() => {
    if (upstream === null) return false;
    try {
      const edited = JSON.parse(editConfig);
      return JSON.stringify(edited) !== JSON.stringify(upstream.server_config);
    } catch {
      // If it doesn't parse, it's dirty (and invalid — validation catches that)
      return editConfig !== JSON.stringify(upstream.server_config, null, 2);
    }
  })();

  // Validate JSON config
  const configJsonValid = (() => {
    try {
      const parsed = JSON.parse(editConfig);
      return typeof parsed === "object" && parsed !== null && ("url" in parsed || "command" in parsed);
    } catch {
      return false;
    }
  })();

  const configJsonSyntaxOk = (() => {
    try {
      JSON.parse(editConfig);
      return true;
    } catch {
      return false;
    }
  })();

  const envVarsDirty =
    Object.keys(pendingTemplateVarChanges.sets).length > 0
    || pendingTemplateVarChanges.deletes.length > 0;

  // Resources are dirty iff any picker value differs from the saved
  // ``upstream.sandbox_resources``. We compare field-by-field rather
  // than reference because ``resources`` is mirrored from the
  // upstream object — a fresh load creates a new identity even when
  // values match.
  const resourcesDirty = (() => {
    if (!upstream?.sandbox_resources || !resources) return false;
    const saved = upstream.sandbox_resources;
    return (
      saved.cpu_vcpus !== resources.cpu_vcpus
      || saved.memory_mb !== resources.memory_mb
      || saved.disk_gb !== resources.disk_gb
      || saved.pids_limit !== resources.pids_limit
      || saved.tmpfs_mb !== resources.tmpfs_mb
      || saved.persistent_disk_enabled !== resources.persistent_disk_enabled
    );
  })();

  const anyDirty =
    cosmeticDirty || authModeDirty || oauthCredsDirty || configDirty
    || envVarsDirty || resourcesDirty;

  const performSave = async (
    overrides?: {
      configJson?: string;
      bufferOverride?: PendingTemplateVarChanges;
    },
  ) => {
    if (!id || !upstream || !anyDirty) return;
    if (configDirty && !configJsonValid) return;
    setSaving(true);
    setResourcesError(null);
    setResourcesField(null);
    try {
      const body: Record<string, unknown> = {};
      if (cosmeticDirty || authModeDirty) {
        body.display_name = editName;
        if (authModeDirty) {
          body.auth_mode = editAuthMode;
        }
      }
      if (clientIdDirty) {
        body.client_id = editClientId;
      }
      if (clientSecretDirty) {
        body.client_secret = editClientSecret;
      }
      const configToSubmit = overrides?.configJson ?? editConfig;
      if (configDirty || overrides?.configJson) {
        body.server_config = JSON.parse(configToSubmit);
      }
      const buffer = overrides?.bufferOverride ?? pendingTemplateVarChanges;
      const bufferDirty =
        Object.keys(buffer.sets).length > 0
        || buffer.deletes.length > 0;
      if (bufferDirty) {
        body.template_var_changes = {
          sets: buffer.sets,
          deletes: buffer.deletes,
        };
      }
      if (resourcesDirty && resources) {
        body.sandbox_resources = {
          cpu_vcpus: resources.cpu_vcpus,
          memory_mb: resources.memory_mb,
          disk_gb: resources.disk_gb,
          pids_limit: resources.pids_limit,
          tmpfs_mb: resources.tmpfs_mb,
          persistent_disk_enabled: resources.persistent_disk_enabled,
        };
      }
      try {
        const updated = await updateUpstream(id, body);
        setUpstream(updated);
        resetEditState(updated);
        setEditing(false);
        setPendingTemplateVarChanges(EMPTY_PENDING_CHANGES);
        void reloadSecrets();
      } catch (e: unknown) {
        // Resource validation rejects with 400 + structured detail
        // ``{message, field, value}``. ``ApiError.detail`` carries
        // the parsed object so we can flag the offending picker
        // control without losing the rest of the dirty state.
        let message = e instanceof Error ? e.message : String(e);
        if (e instanceof ApiError && e.detail
          && typeof e.detail === "object") {
          const det = e.detail as { field?: unknown; message?: unknown };
          if (typeof det.field === "string") setResourcesField(det.field);
          if (typeof det.message === "string") message = det.message;
        }
        setResourcesError(message);
      }
    } finally {
      setSaving(false);
    }
  };

  /** Scan the JSON for raw credentials before save; if findings,
   *  hold the request until the SecretDetectionDialog resolves. */
  const handleSave = async () => {
    if (!id) return;
    if (configDirty) {
      try {
        const parsed = JSON.parse(editConfig) as Record<string, unknown>;
        const inner = parsed.url || parsed.command
          ? parsed
          : (Object.values(parsed)[0] as Record<string, unknown>);
        const env = inner.env as Record<string, string> | undefined;
        const headers = inner.headers as Record<string, string> | undefined;
        const findings = scanConfigForSecrets(env, headers);
        if (findings.length > 0 && !isSecretDetectionDismissed(id)) {
          setPendingFindings(findings);
          return;
        }
      } catch {
        // If parsing fails, fall through to performSave which has its
        // own validation.
      }
    }
    await performSave();
  };

  const handleDetectionResolved = async (
    extractions: Array<{
      finding: ScanFinding;
      envVarName: string;
      value: string;
      isSecret: boolean;
    }>,
    resolution: DetectionResolution,
  ) => {
    setPendingFindings([]);
    // The dialog's internal "Don't show again" checkbox may have
    // flipped the localStorage flag — pull the new value back into
    // React state so the §H undo link reactively appears.
    if (id) setDetectionDismissed(isSecretDetectionDismissed(id));
    if (resolution.kind === "dismissed") return;
    if (extractions.length === 0) {
      await performSave();
      return;
    }
    if (!id) return;
    // Apply each move: stage the env var into the deferred buffer
    // and rewrite the JSON. The combined SETTINGS Save flushes both
    // atomically (config write + env-var changes in one PUT).
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(editConfig) as Record<string, unknown>;
    } catch {
      return;
    }
    const innerKey = parsed.url || parsed.command ? null : Object.keys(parsed)[0];
    const inner = innerKey === null
      ? parsed
      : (parsed[innerKey] as Record<string, unknown>);
    const newEnv: Record<string, string> = { ...((inner.env as Record<string, string>) ?? {}) };
    const newHeaders: Record<string, string> = { ...((inner.headers as Record<string, string>) ?? {}) };
    const nextSets = { ...pendingTemplateVarChanges.sets };
    const nextDeletes = pendingTemplateVarChanges.deletes.slice();
    for (const { finding, envVarName, value, isSecret } of extractions) {
      const placeholder = `\${${envVarName}}`;
      if (finding.field === "env") {
        newEnv[finding.key] = newEnv[finding.key].replace(value, placeholder);
      } else {
        newHeaders[finding.key] = newHeaders[finding.key].replace(value, placeholder);
      }
      // ``isSecret`` honours the per-finding toggle (defaults to true
      // because the value matched a secret-shaped pattern; user can
      // opt out for false positives). For names that already exist
      // on the server the dialog locks the field, surfacing the
      // replacement intent — the buffer flush turns into a replace
      // (the repository preserves ``is_secret`` on replace, so the
      // toggle is moot in that case).
      nextSets[envVarName] = { value, is_secret: isSecret };
      const idx = nextDeletes.indexOf(envVarName);
      if (idx !== -1) nextDeletes.splice(idx, 1);
    }
    if (Object.keys(newEnv).length > 0) inner.env = newEnv;
    if (Object.keys(newHeaders).length > 0) inner.headers = newHeaders;
    const newConfigJson = JSON.stringify(parsed, null, 2);
    setEditConfig(newConfigJson);
    const nextBuffer: PendingTemplateVarChanges = {
      sets: nextSets,
      deletes: nextDeletes,
    };
    setPendingTemplateVarChanges(nextBuffer);
    await performSave({
      configJson: newConfigJson,
      bufferOverride: nextBuffer,
    });
  };

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;
  if (!upstream) return <p className="text-red-500">{t("upstreamDetail.notFound")}</p>;

  return (
    <div>
      <Link
        to={`/orgs/${orgSlug}/admin/upstream`}
        className="inline-flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-700 mb-4"
      >
        <ArrowLeft size={14} /> {t("upstreamDetail.backToTeamMcps")}
      </Link>

      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-semibold text-zinc-900">
            {editing ? editName || upstream.display_name : upstream.display_name}
          </h2>
          <StatusBadge
            connected={upstream.ready}
            disconnectReason={upstream.disconnect_reason}
            starting={upstream.starting}
            label={upstreamStatusLabel(upstream, user?.email ?? null, t)}
          />
          <UpstreamActionButtons
            id={upstream.id}
            ready={upstream.ready}
            transport={upstream.transport}
            authMode={upstream.auth_mode}
            starting={upstream.starting}
            reload={reload}
            onOptimisticReset={() => {
              // Synchronous, in-process reset so the operator sees
              // the cleared state the instant they click Start —
              // before the API roundtrip + the ``MIN_DELAY`` busy
              // window. The subsequent ``reload()`` then reconciles
              // with server truth (typically a fresh ``starting=true``
              // snapshot, or a new failure if the command fast-fails
              // in <1 s like a python ``SyntaxError`` does).
              setUpstream((prev) =>
                prev ? { ...prev, disconnect_reason: null, starting: true } : prev,
              );
              setLogsResetEpoch((n) => n + 1);
            }}
          />
        </div>
        {/* The ID readout used to live here as a small subtitle; it
            now sits at the top of the Overview card below alongside
            the rest of the at-a-glance metadata. */}
      </div>

      {/* Dirty banner — running session was started against a config
          snapshot that no longer matches the saved state. Replaces
          the legacy post-save RestartRequiredDialog modal. */}
      <DirtyConfigBanner
        visible={upstream.is_dirty}
        upstreamId={upstream.id}
        configHash={upstream.config_hash ?? ""}
        transport={upstream.transport}
      />

      {/* Resource picker (CPU/Memory/Volume) lives inside the
          settings card below for stdio upstreams; the previous
          standalone "Sandbox" tab has been removed.
          See plan: serene-beaming-tulip §Phase 3. */}

      {(!upstream.ready && upstream.disconnect_reason) && (() => {
        const reason = upstream.disconnect_reason;
        if (!reason) return null;
        const hasKey = `disconnectReason.${reason}` in en;
        return (
          <div className={`mb-4 p-3 text-sm rounded border ${
            hasKey ? "bg-amber-50 text-amber-700 border-amber-200" : "bg-red-50 text-red-700 border-red-200"
          }`}>
            {hasKey ? t(`disconnectReason.${reason}` as TranslationKey) : reason}
          </div>
        );
      })()}

      {/* Settings — three cards (Identity / Connection / Resources)
          with a single page-level toolbar at the top. The card
          structure is identical across view and edit modes; the
          body of each card swaps controls for readouts based on
          ``editing``, so the operator's eye lands on the same row
          in the same place across modes. */}
      <div className="mb-6">
        <div className="mb-3 flex items-center gap-3">
          <span className="text-xs text-zinc-400">
            {upstream.transport === "stdio"
              ? "Changes apply on the next start."
              : "Changes apply on the next reconnect."}
          </span>
          <div className="ml-auto flex items-center gap-2">
            {editing ? (
              <>
                <button
                  onClick={handleSave}
                  disabled={!anyDirty || saving || !editName || (configDirty && !configJsonValid)}
                  className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded not-disabled:hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {saving ? t("upstreamDetail.saving") : t("upstreamDetail.save")}
                </button>
                <button
                  onClick={cancelEdit}
                  className="px-3 py-1.5 bg-zinc-100 text-zinc-700 text-sm rounded hover:bg-zinc-200"
                >
                  {t("common.cancel")}
                </button>
              </>
            ) : (
              <button
                onClick={enterEdit}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
              >
                <Pencil size={14} /> {t("upstreamDetail.edit")}
              </button>
            )}
          </div>
        </div>

        {/* Identity */}
        <SettingsCard title="Overview">
          <SettingsField label={t("upstreams.formId")}>
            <span className="text-sm font-mono text-zinc-800">{upstream.id}</span>
          </SettingsField>

          <SettingsField label={t("upstreams.formDisplayName")}>
            {editing ? (
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                placeholder={t("upstreamDetail.placeholderDisplayName")}
                className="w-full max-w-md px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            ) : (
              <span className="text-sm text-zinc-800">{upstream.display_name}</span>
            )}
          </SettingsField>

          <SettingsField label={t("upstreamDetail.labelTransport")}>
            <TransportBadge transport={upstream.transport} withDescription />
          </SettingsField>

          {/* URL (HTTP) or Command (stdio) readout — the operator's
              eye-level "what is this MCP" answer. Always read-only;
              the editable form lives in the JSON config inside the
              Configuration card below. */}
          {upstream.transport === "streamable_http" && upstream.url && (
            <SettingsField label="URL">
              <span className="text-sm font-mono text-zinc-800 break-all">
                {upstream.url}
              </span>
            </SettingsField>
          )}
          {upstream.transport === "stdio" && upstream.command && (
            <SettingsField label="Command">
              <span className="text-sm font-mono text-zinc-800 break-all">
                {upstream.command}
              </span>
            </SettingsField>
          )}

          {/* User authentication mode — sits in Overview as part of
              the at-a-glance metadata for the upstream. Stdio MCPs
              are always service_account (OAuth modes require an HTTP
              endpoint the gateway can drive the OAuth handshake
              against, which stdio sandboxes can't host); the backend
              rejects stdio + OAuth at write time, so the picker /
              readout is suppressed entirely for stdio. The OAuth
              client-credentials fold that pairs with this picker
              still lives down in Configuration where the rest of the
              connection-shape inputs sit. */}
          {upstream.transport !== "stdio" && (
            <SettingsField label={t("upstreamDetail.labelAuthentication")}>
              {editing ? (
                <div className="space-y-2 max-w-md">
                  {AUTH_MODES.map((mode) => (
                    <label
                      key={mode}
                      className={`flex items-start gap-2 p-2 rounded border cursor-pointer ${
                        editAuthMode === mode
                          ? "border-blue-300 bg-blue-50"
                          : "border-zinc-200 bg-white hover:border-zinc-300"
                      }`}
                    >
                      <input
                        type="radio"
                        name="authMode"
                        value={mode}
                        checked={editAuthMode === mode}
                        onChange={() => setEditAuthMode(mode)}
                        className="mt-0.5"
                      />
                      <div>
                        <div className="text-sm font-medium text-zinc-800">
                          {t(`authMode.${mode}` as TranslationKey)}
                        </div>
                        <div className="text-xs text-zinc-500">
                          {t(`authMode.${mode}.description` as TranslationKey)}
                        </div>
                      </div>
                    </label>
                  ))}
                </div>
              ) : (
                <Tooltip>
                  <TooltipTrigger className="cursor-help inline-flex items-center px-1.5 py-0.5 rounded text-xs bg-blue-50 text-blue-700">
                    {t(`authMode.${upstream.auth_mode}` as TranslationKey)}
                  </TooltipTrigger>
                  <TooltipContent>
                    {t(`authMode.${upstream.auth_mode}.description` as TranslationKey)}
                  </TooltipContent>
                </Tooltip>
              )}
            </SettingsField>
          )}
        </SettingsCard>

        {/* Configuration — JSON, Variables, Sandbox files, and the
            OAuth client-credentials fold (visible in edit mode for
            OAuth-shaped upstreams). The user-auth mode itself moved
            up into Overview as part of the at-a-glance metadata; the
            credentials fold stays here because it's a connection-
            shape input, not at-a-glance info. */}
        <SettingsCard title="Configuration">
          <SettingsField
            label={t("upstreamDetail.configuration")}
            hint={editing ? (
              <>
                {/* Validation errors render above the informational
                    hint (and in red) so an operator with a syntax
                    error sees the actionable message first. */}
                {editConfig && !configJsonSyntaxOk && (
                  <FieldHint error>{t("upstreamDetail.errorInvalidJson")}</FieldHint>
                )}
                {editConfig && configJsonSyntaxOk && !configJsonValid && (
                  <FieldHint error>{t("upstreamDetail.errorMissingUrlOrCommand")}</FieldHint>
                )}
                <FieldHint>
                  Use <code>{'${MY_VAR}'}</code> to reference a variable from the list below. Use <code>{'\\${VAR}'}</code> to prevent expansion.
                </FieldHint>
              </>
            ) : undefined}
          >
            {editing ? (
              <JsonConfigEditor
                value={editConfig}
                onChange={setEditConfig}
                rows={Math.max(6, editConfig.split("\n").length + 1)}
              />
            ) : (
              <pre className="p-3 bg-zinc-50 rounded text-xs font-mono text-zinc-700 overflow-x-auto">
                {JSON.stringify(upstream.server_config, null, 2)}
              </pre>
            )}
          </SettingsField>
          {id && (
            <div>
              {editing ? (
                <TemplateVarsManager
                  upstreamId={id}
                  references={(() => {
                    let configRefs: TemplateVarReference[];
                    try {
                      configRefs = findReferences(JSON.parse(editConfig));
                    } catch {
                      configRefs = [];
                    }
                    // Target_paths accept the same ``${...}``
                    // references env-var values do; an undefined
                    // reference in either surfaces in the same
                    // amber callout.
                    return [
                      ...configRefs,
                      ...findReferencesInSandboxFiles(sandboxFiles),
                    ];
                  })()}
                  pendingChanges={pendingTemplateVarChanges}
                  onPendingChange={setPendingTemplateVarChanges}
                  handleRef={templateVarsManagerRef}
                  isStdio={upstream.transport === "stdio"}
                />
              ) : (
                <>
                  <TemplateVarsManager
                    upstreamId={id}
                    references={(() => {
                      let configRefs: TemplateVarReference[];
                      try {
                        configRefs = findReferences(upstream.server_config);
                      } catch {
                        configRefs = [];
                      }
                      return [
                        ...configRefs,
                        ...findReferencesInSandboxFiles(sandboxFiles),
                      ];
                    })()}
                    readOnly
                    isStdio={upstream.transport === "stdio"}
                  />
                  {detectionDismissed && (
                    <button
                      type="button"
                      onClick={showDetectionAgain}
                      className="mt-2 text-xs text-zinc-500 hover:text-zinc-800 underline"
                    >
                      Show password detection again
                    </button>
                  )}
                </>
              )}
            </div>
          )}
          {/* Sandbox files (stdio only) — sibling of Variables.
              Files don't participate in ${...} substitution; their
              target_path accepts the same ${...} references env-var
              values do (system + user vars). Server-direct (no
              buffer): Add/Replace/Delete hit the API immediately.
              ``onFilesChanged`` reloads the page-level summary list
              so the Variables card re-scans target_paths for
              undefined-Variable detection. The runtime hash includes
              file metadata so the dirty banner fires on file edits
              the same way it does on Variable edits. */}
          {id && upstream.transport === "stdio" && (
            <div>
              <SandboxFilesManager
                upstreamId={id}
                readOnly={!editing}
                onFilesChanged={() => void reloadSandboxFiles()}
              />
            </div>
          )}

          {/* OAuth client credentials — collapsible in edit mode for
              OAuth-shaped upstreams; flat readout in view mode when
              the saved record carries any. The block is irrelevant
              for ``service_account`` mode and is suppressed there.
              The auth-mode picker that gates this fold lives up in
              the Overview card. */}
          {editing && (editAuthMode === "admin_oauth" || editAuthMode === "per_user_oauth") && (
            <div className="border border-zinc-200 rounded">
              <button
                onClick={() => setConfigOpen(!configOpen)}
                className="flex items-center gap-2 w-full px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 text-left rounded"
              >
                {configOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                OAuth client credentials
              </button>
              {configOpen && (
                <div className="px-3 pb-3 border-t border-zinc-200 space-y-2 pt-3">
                  {upstream.oauth_app_domain && !editClientId && (
                    <p className="text-xs text-green-600 bg-green-50 border border-green-200 rounded p-2">
                      Using instance credentials for <span className="font-mono">{upstream.oauth_app_domain}</span>.
                      Enter credentials below to override.
                    </p>
                  )}
                  <p className="text-xs text-zinc-500">
                    Required for servers that don't support dynamic client registration (e.g. GitHub).
                  </p>
                  <input
                    type="text"
                    value={editClientId}
                    onChange={(e) => setEditClientId(e.target.value)}
                    placeholder="OAuth Client ID"
                    className="w-full max-w-md px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
                  />
                  <input
                    type="password"
                    value={editClientSecret}
                    onChange={(e) => setEditClientSecret(e.target.value)}
                    placeholder={upstream.has_client_secret ? "••••••••  (leave blank to keep)" : "OAuth Client Secret"}
                    className="w-full max-w-md px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
                  />
                </div>
              )}
            </div>
          )}
          {!editing && (upstream.client_id || upstream.has_client_secret || upstream.oauth_app_domain) && (
            <SettingsField label="OAuth client credentials">
              {upstream.oauth_app_domain ? (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-green-50 text-green-700 border border-green-200">
                  Using instance credentials for <span className="font-mono">{upstream.oauth_app_domain}</span>
                </span>
              ) : (
                <div className="space-y-1 text-sm">
                  {upstream.client_id && (
                    <div className="flex items-center gap-2">
                      <span className="text-zinc-500">Client ID:</span>
                      <span className="text-zinc-800 font-mono">{upstream.client_id}</span>
                    </div>
                  )}
                  {upstream.has_client_secret && (
                    <div className="flex items-center gap-2">
                      <span className="text-zinc-500">Client Secret:</span>
                      <span className="text-zinc-800">••••••••</span>
                    </div>
                  )}
                </div>
              )}
            </SettingsField>
          )}
        </SettingsCard>

        {/* Resources — stdio only; no card for HTTP MCPs since the
            sandbox grid is irrelevant. */}
        {upstream.transport === "stdio" && (
          <SettingsCard title="Sandbox">
            <SettingsField label="Resources">
              {editing && resources && capabilities ? (
                <>
                  <SandboxComboSelect
                    capabilities={capabilities}
                    value={resources}
                    onChange={(combo) => setResources({ ...resources, ...combo })}
                    hasError={!!resourcesField}
                    borderDefault="border-zinc-200"
                    command={upstream.command ?? undefined}
                  />
                  {capabilities.supports_persistent_disk && (
                    <label className="mt-3 inline-flex items-center gap-2 text-sm text-zinc-700">
                      <input
                        type="checkbox"
                        checked={resources.persistent_disk_enabled}
                        onChange={(e) =>
                          setResources({
                            ...resources,
                            persistent_disk_enabled: e.target.checked,
                          })
                        }
                      />
                      Persistent volume
                      <span className="text-xs text-zinc-400">
                        (mount /data across sessions)
                      </span>
                    </label>
                  )}
                  {resourcesError && (
                    <p className="mt-2 text-sm text-red-700">{resourcesError}</p>
                  )}
                </>
              ) : !editing && upstream.sandbox_resources ? (
                <div className="text-sm text-zinc-800 space-y-0.5">
                  <div>
                    {upstream.sandbox_resources.cpu_vcpus} vCPU
                    {" / "}
                    {upstream.sandbox_resources.memory_mb >= 1024 &&
                    upstream.sandbox_resources.memory_mb % 1024 === 0
                      ? `${upstream.sandbox_resources.memory_mb / 1024} GiB`
                      : `${upstream.sandbox_resources.memory_mb} MiB`}
                    {upstream.sandbox_resources.disk_gb > 0
                      ? ` / ${upstream.sandbox_resources.disk_gb} GiB disk`
                      : ""}
                  </div>
                  {upstream.sandbox_resources.persistent_disk_enabled && (
                    <div className="text-xs text-zinc-500">
                      Persistent volume enabled
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-xs text-zinc-500 italic">
                  No sandbox resources configured.
                </p>
              )}
            </SettingsField>
            <LogViewer
              upstreamId={upstream.id}
              resetEpoch={logsResetEpoch}
            />
          </SettingsCard>
        )}

        {/* Remove — only meaningful in edit mode. Sits at page-
            level below the cards so it doesn't compete visually
            with the per-card actions. */}
        {editing && (
          <div className="flex justify-end">
            <button
              onClick={handleRemove}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-red-500 border border-red-200 rounded hover:bg-red-50 hover:border-red-300"
            >
              <Trash2 size={12} />
              {t("common.remove")}
            </button>
          </div>
        )}
      </div>

      {/* Execution logs are rendered inside the Sandbox card above
          (alongside the Resources subsection); they're stdio-only
          and the card itself is conditional on
          ``upstream.transport === "stdio"``. */}

      {/* Server info */}
      {upstream.server_info && (
        <div className="mb-6">
          <h3 className="text-lg font-medium text-zinc-900 mb-3">
            {t("upstreamDetail.serverInfo")}
          </h3>
          <div className="border border-zinc-200 rounded-lg p-4 text-sm space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-zinc-500">Name:</span>
              <span className="text-zinc-800">{upstream.server_info.title ?? upstream.server_info.name}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-zinc-500">Version:</span>
              <span className="font-mono text-zinc-800">{upstream.server_info.version}</span>
            </div>
          </div>
        </div>
      )}

      {/* Tools table */}
      <div className="flex items-center gap-3 mb-3">
        <h3 className="text-lg font-medium text-zinc-900">
          {t("upstreamDetail.toolsCount", { count: tools.length })}
        </h3>
        <ActionButton
          variant="neutral"
          onClick={handleRefreshTools}
          disabled={!upstream.ready || upstream.starting || refreshingTools}
        >
          {refreshingTools ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <RefreshCw size={12} />
          )}
          {t("upstreamDetail.refreshTools")}
        </ActionButton>
      </div>
      {tools.length > 0 ? (
        <div className="border border-zinc-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 text-zinc-600">
              <tr>
                <th className="text-left px-4 py-2 font-medium w-[30%]">
                  {t("upstreamDetail.headerToolName")}
                </th>
                <th className="text-left px-4 py-2 font-medium">
                  {t("upstreamDetail.headerDescription")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {tools.map((tool) => (
                <ToolRow key={tool.prefixed_name} tool={tool} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-zinc-400 text-sm">
          {t("upstreamDetail.noTools")}
        </p>
      )}
      <ConfirmDialog {...dialogProps} />
      <SecretDetectionDialog
        open={pendingFindings.length > 0}
        findings={pendingFindings}
        upstreamId={id ?? ""}
        existingNames={effectiveTemplateVarNames}
        onResolve={(extractions, resolution) => {
          void handleDetectionResolved(extractions, resolution);
        }}
        resolveValue={(finding) => {
          // Recover the original value out of editConfig.
          try {
            const parsed = JSON.parse(editConfig) as Record<string, unknown>;
            const inner = parsed.url || parsed.command
              ? parsed
              : (Object.values(parsed)[0] as Record<string, unknown>);
            const source =
              finding.field === "env"
                ? (inner.env as Record<string, string> | undefined)
                : (inner.headers as Record<string, string> | undefined);
            return source?.[finding.key] ?? finding.matchPreview;
          } catch {
            return finding.matchPreview;
          }
        }}
      />
    </div>
  );
}
