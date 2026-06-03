import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { useOrgSlug } from "../../hooks/useOrgSlug";
import { addUpstream, removeUpstream } from "../../api/admin";
import { fetchSandboxCapabilities } from "../../api/sandbox";
import { SandboxComboSelect } from "../../components/SandboxComboSelect";
import { firstEnabledCombo } from "../../lib/sandbox-combo";
import type { SandboxCapabilitiesResponse } from "../../api/types";
import { useUpstreams } from "../../hooks/useUpstreams";
import { UpstreamCapacityPills } from "../../components/UpstreamCapacityPills";
import { UpstreamActionButtons } from "../../components/UpstreamActionButtons";
import { useTranslation, type TranslationKey } from "../../i18n/index";
import { en } from "../../i18n/en";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { ImportMcpModal } from "../../components/ImportMcpModal";
import { StdioPromoDialog } from "../../components/StdioPromoDialog";
import { track } from "../../lib/analytics";
import {
  PLAN_UPSTREAM_LIMITS,
  handlePlanLimitError,
  maybeHandlePlanLimit,
} from "../../lib/planLimits";
import { PlanLimitError } from "../../api/client";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import { useAuth } from "../../hooks/useAuth";
import { useFeatures } from "../../hooks/useFeatures";
import { useEventSource } from "../../hooks/useEventSource";
import { UpstreamFavicon } from "../../components/UpstreamFavicon";
import { AuthBadge } from "../../components/AuthBadge";
import { TransportBadge } from "../../components/TransportBadge";
import { inferIdFromCommand, inferIdFromUrl } from "../../lib/upstream-inference";
import { StatusBadge } from "../../components/StatusBadge";
import { upstreamStatusLabel } from "../../lib/upstreamStatusLabel";
import { Trash2, Upload, Plus, ChevronDown, ChevronRight, ArrowLeft, ArrowRight, Loader2 } from "lucide-react";
import IdInput from "../../components/ui/id-input";
import { FieldHint } from "../../components/ui/field-hint";
import { SettingsCard, SettingsField } from "../../components/SettingsCard";
import { JsonConfigEditor } from "../../components/JsonConfigEditor";
import {
  TemplateVarsManager,
  type BufferedTemplateVar,
  type TemplateVarsManagerHandle,
} from "../../components/TemplateVarsManager";
import {
  SecretDetectionDialog,
  isSecretDetectionDismissed,
  type DetectionResolution,
} from "../../components/SecretDetectionDialog";
import { scanConfigForSecrets, type ScanFinding } from "../../lib/secret-detection";
import { findReferences } from "../../lib/template-var-references";

export function UpstreamsPage() {
  const { t } = useTranslation();
  const orgSlug = useOrgSlug();
  const { confirm, dialogProps } = useConfirm();
  const { upstreams, isLoading: loading, httpCount, stdioCount, setUpstreams, invalidate } = useUpstreams();
  const { allowStdioMcp } = useFeatures();
  const { user } = useAuth();
  const [showForm, setShowForm] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showStdioPromo, setShowStdioPromo] = useState(false);

  // Form state
  const [formStep, setFormStep] = useState<1 | 2>(1);
  const [formTab, setFormTab] = useState<"url" | "json">("url");
  const [formId, setFormId] = useState("");
  const [formName, setFormName] = useState("");
  const [formNameTouched, setFormNameTouched] = useState(false);
  const [formIdTouched, setFormIdTouched] = useState(false);
  const [formUrl, setFormUrl] = useState("");
  const [formAuthMode, setFormAuthMode] = useState("per_user_oauth");
  const [formJson, setFormJson] = useState("");
  const [formClientId, setFormClientId] = useState("");
  const [formClientSecret, setFormClientSecret] = useState("");
  const [formAdvancedOpen, setFormAdvancedOpen] = useState(false);
  const [formError, setFormError] = useState("");
  const [adding, setAdding] = useState(false);
  // Per-MCP env vars buffered while the wizard is open. Submitted
  // with the create call; dropped on Cancel. Each entry carries the
  // value + the create-time ``is_secret`` choice.
  const [formTemplateVars, setFormTemplateVars] = useState<Record<string, BufferedTemplateVar>>({});
  const templateVarsManagerRef = useRef<TemplateVarsManagerHandle | null>(null);
  // Pending detection-dialog state — when ``handleAdd`` finds raw
  // credentials in the JSON, we hold the request here until the
  // dialog resolves (move / keep / dismiss).
  const [pendingFindings, setPendingFindings] = useState<ScanFinding[]>([]);
  // Sandbox resources picker state. Lazy-loaded the first time step 2
  // is shown for a stdio MCP; the picker is hidden for HTTP.
  const [capabilities, setCapabilities] = useState<SandboxCapabilitiesResponse | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [formCpu, setFormCpu] = useState<number | null>(null);
  const [formMemory, setFormMemory] = useState<number | null>(null);
  const [formDisk, setFormDisk] = useState<number | null>(null);
  const [formPersistentDisk, setFormPersistentDisk] = useState(false);
  const [resourcesField, setResourcesField] = useState<string | null>(null);

  const reload = () => { invalidate(); };

  // Step-2 stdio detection. The URL tab always means HTTP. The JSON
  // tab means stdio when the parsed payload carries ``command``.
  const formIsStdio: boolean = (() => {
    if (formTab === "url") return false;
    try {
      const parsed = JSON.parse(formJson);
      const inner = parsed?.command || parsed?.url ? parsed
        : Object.values(parsed ?? {})[0];
      return Boolean((inner as { command?: unknown } | null)?.command);
    } catch {
      return false;
    }
  })();

  // The parsed stdio command, for the resource picker's docker-sizing
  // warning. Same unwrap as ``formIsStdio``; "" when not stdio / unparsable.
  const formCommand: string = (() => {
    if (formTab === "url") return "";
    try {
      const parsed = JSON.parse(formJson);
      const inner = parsed?.command || parsed?.url ? parsed
        : Object.values(parsed ?? {})[0];
      const cmd = (inner as { command?: unknown } | null)?.command;
      return typeof cmd === "string" ? cmd : "";
    } catch {
      return "";
    }
  })();

  // Lazy-fetch the active provider's grid the first time step 2 is
  // shown for a stdio MCP. The detail page does the same fetch for
  // the post-create edit picker; we hit the same endpoint here so the
  // form's options match what's allowed at save time.
  useEffect(() => {
    if (formStep !== 2 || !formIsStdio || capabilities !== null) return;
    setCapabilitiesError(null);
    fetchSandboxCapabilities()
      .then((caps) => {
        setCapabilities(caps);
        // Seed from the first PLAN-ENABLED combo, not ``axis[0]``:
        // the per-axis lists aren't plan-filtered, so their first
        // entries can be an off-plan pair the backend gate rejects on
        // submit. See ``firstEnabledCombo`` for the full rationale.
        const seed = firstEnabledCombo(caps);
        setFormCpu((cur) => cur ?? seed?.cpu_vcpus ?? caps.allowed_cpu_vcpus[0] ?? 1);
        setFormMemory((cur) => cur ?? seed?.memory_mb ?? caps.allowed_memory_mb[0] ?? 1024);
        setFormDisk((cur) => cur ?? seed?.disk_gb ?? caps.allowed_disk_gb[0] ?? 0);
      })
      .catch((e: unknown) => {
        setCapabilitiesError(
          e instanceof Error ? e.message : "Failed to load capabilities",
        );
      });
  }, [formStep, formIsStdio, capabilities]);

  // Re-fetch the upstream list whenever the backend signals a policy
  // change. The post-connect catalog refresh runs in the background
  // and emits ``policy_changed`` once it finishes — that's what flips
  // the row's "Fetching info" indicator off and pulls in the real
  // tool_count without forcing the user to refresh the page.
  useEventSource({
    policy_changed: () => { invalidate(); },
  });

  const existingIds = new Set(upstreams.map((u) => u.id));
  const idExists = formId !== "" && existingIds.has(formId);

  const idToDisplayName = (id: string): string =>
    id.replace(/[-_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  const handleNext = () => {
    setFormError("");
    // Pre-flight plan cap check fires here, at the first "Next",
    // because this is the earliest moment the wizard knows the
    // transport. Catching at-cap here means the user doesn't fill
    // out display name / auth / env vars only to be told at submit
    // that they can't add this MCP. The same gate fires server-side
    // on the eventual POST as a defense-in-depth check.
    const plan = user?.current_org?.plan ?? "free";
    const limits = PLAN_UPSTREAM_LIMITS[plan];
    const fireCapGate = (gate: "max_http_upstreams" | "max_stdio_upstreams",
                        current: number, limit: number) => {
      handlePlanLimitError(
        new PlanLimitError(
          gate === "max_http_upstreams"
            ? `Free plans are limited to ${limit} remote MCPs.`
            : `Free plans are limited to ${limit} self-hosted MCP.`,
          gate, current, limit,
        ),
        { source: "add_mcp_wizard_next" },
      );
      // Don't close the form — the user might cancel the wizard
      // themselves; closing here would lose their typed JSON / URL.
    };
    if (formTab === "url") {
      let url = formUrl.trim();
      if (url && !url.includes("://") && /^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(\/.*)?$/.test(url)) {
        url = `https://${url}`;
        setFormUrl(url);
      }
      if (!url) { setFormError("Please enter a URL."); return; }
      try { new URL(url); } catch { setFormError(t("upstreams.errorInvalidUrl")); return; }
      if (limits.http !== null && httpCount >= limits.http) {
        fireCapGate("max_http_upstreams", httpCount, limits.http);
        return;
      }
      if (!formIdTouched) {
        const inferred = inferIdFromUrl(url);
        setFormId(inferred);
        if (!formNameTouched) setFormName(idToDisplayName(inferred));
      }
    } else {
      const unwrapped = unwrapJson(formJson);
      if (!unwrapped) {
        try { JSON.parse(formJson); setFormError(t("upstreams.errorMissingUrlOrCommand")); }
        catch { setFormError(t("upstreams.errorInvalidJson")); }
        return;
      }
      // If wrapped like {"slack": {...}}, rewrite formJson to inner and use key as ID
      if (unwrapped.key) {
        setFormJson(JSON.stringify(unwrapped.inner, null, 2));
        if (!formIdTouched) {
          setFormId(unwrapped.key);
          if (!formNameTouched) setFormName(idToDisplayName(unwrapped.key));
        }
      }
      const parsed = parseJson(JSON.stringify(unwrapped.inner));
      if (!parsed) return;

      // Stdio MCP gating + analytics. Block the flow with the promo
      // dialog when stdio is disabled; in either case fire one
      // ``stdio_mcp_attempted`` event so we can size demand.
      if (parsed.command) {
        track("stdio_mcp_attempted", { was_blocked: !allowStdioMcp });
        if (!allowStdioMcp) {
          setShowStdioPromo(true);
          return;
        }
        if (limits.stdio !== null && stdioCount >= limits.stdio) {
          fireCapGate("max_stdio_upstreams", stdioCount, limits.stdio);
          return;
        }
      } else if (parsed.url) {
        if (limits.http !== null && httpCount >= limits.http) {
          fireCapGate("max_http_upstreams", httpCount, limits.http);
          return;
        }
      }

      if (parsed.authMode) setFormAuthMode(parsed.authMode);
      // If no key-based ID, try inferring from URL or command
      if (!formIdTouched && !unwrapped.key) {
        let inferred = "";
        if (parsed.url) {
          inferred = inferIdFromUrl(parsed.url);
        } else if (parsed.command) {
          inferred = inferIdFromCommand(parsed.command, parsed.args ?? []);
        }
        if (inferred) {
          setFormId(inferred);
          if (!formNameTouched) setFormName(idToDisplayName(inferred));
        }
      }
    }
    setFormStep(2);
  };

  /**
   * Unwrap any of the three accepted shapes into a single server entry:
   *   - Direct:        ``{"url"|"command": ..., ...}``
   *   - Single-id:     ``{"slack": {"url"|"command": ..., ...}}``
   *   - mcpServers:    ``{"mcpServers": {"slack": {...}}}`` (one server only)
   *
   * Anything else (multiple servers under ``mcpServers``, no
   * ``url``/``command``, etc.) returns ``null``.
   */
  const unwrapJson = (json: string): { key: string | null; inner: Record<string, unknown> } | null => {
    try {
      const parsed = JSON.parse(json);
      if (typeof parsed !== "object" || parsed === null) return null;
      // Direct format: {"url":"..."} or {"command":"..."}
      if (parsed.url || parsed.command) return { key: null, inner: parsed };
      // mcpServers wrapper: {"mcpServers": {"slack": {"url":"..."}}}.
      // Only the one-server form auto-unwraps; multi-server pastes
      // need to be split first.
      const wrapper = parsed.mcpServers;
      if (
        wrapper && typeof wrapper === "object"
        && Object.keys(wrapper).length === 1
      ) {
        const [wrapperKey] = Object.keys(wrapper);
        const wrapperInner = (wrapper as Record<string, unknown>)[wrapperKey];
        if (
          wrapperInner && typeof wrapperInner === "object"
          && ((wrapperInner as Record<string, unknown>).url
            || (wrapperInner as Record<string, unknown>).command)
        ) {
          return {
            key: wrapperKey,
            inner: wrapperInner as Record<string, unknown>,
          };
        }
      }
      // Single-id wrapper: {"slack": {"url":"..."}}
      const keys = Object.keys(parsed);
      if (keys.length === 1) {
        const inner = parsed[keys[0]];
        if (typeof inner === "object" && inner !== null && (inner.url || inner.command)) {
          return { key: keys[0], inner };
        }
      }
      return null;
    } catch { return null; }
  };



  // Note: stdio MCPs are no longer flagged here — the block fires on the
  // Next click via a promotional dialog (so we can pitch the upcoming
  // stdio support and capture demand via the stdio_mcp_attempted event).
  const jsonParseResult = formTab === "json" && formJson !== "" ? (() => {
    try {
      JSON.parse(formJson);
    } catch { return "syntax_error" as const; }
    const unwrapped = unwrapJson(formJson);
    if (!unwrapped) return "missing_fields" as const;
    return "ok" as const;
  })() : null;

  interface ParsedJson {
    url?: string;
    headers?: Record<string, string>;
    command?: string;
    args?: string[];
    env?: Record<string, string>;
    token?: string;
    authMode?: string;
  }

  const parseJson = (json: string): ParsedJson | null => {
    try {
      const parsed = JSON.parse(json);
      const result: ParsedJson = {};
      if (parsed.url) {
        result.url = parsed.url;
        if (parsed.headers) result.headers = parsed.headers;
        const authHeader = parsed.headers?.Authorization || parsed.headers?.authorization;
        if (authHeader && typeof authHeader === "string" && authHeader.startsWith("Bearer ")) {
          result.token = authHeader.slice(7);
          result.authMode = "service_account";
        } else {
          result.authMode = "per_user_oauth";
        }
      } else if (parsed.command) {
        result.command = parsed.command;
        if (Array.isArray(parsed.args)) result.args = parsed.args;
        if (parsed.env && typeof parsed.env === "object") result.env = parsed.env;
        result.authMode = "service_account";
      } else {
        setFormError(t("upstreams.errorMissingUrlOrCommand"));
        return null;
      }
      return result;
    } catch {
      setFormError(t("upstreams.errorInvalidJson"));
      return null;
    }
  };

  const performAdd = async (
    overrides?: {
      env?: Record<string, string>;
      headers?: Record<string, string>;
      template_vars?: Record<string, BufferedTemplateVar>;
    },
  ) => {
    const transport = formTab === "url" || formUrl ? "streamable_http" : "stdio";
    track("upstream_add_attempted", {
      transport,
      entry_method: formTab,
    });
    setFormError("");
    let url: string | undefined = formUrl || undefined;
    let authMode = formAuthMode;
    let token: string | undefined;
    let command: string | undefined;
    let args: string[] | undefined;
    let env: Record<string, string> | undefined;
    let headers: Record<string, string> | undefined;

    if (formTab === "json") {
      const parsed = parseJson(formJson);
      if (!parsed) return;
      url = parsed.url;
      command = parsed.command;
      args = parsed.args;
      env = parsed.env;
      headers = parsed.headers;
      authMode = formAuthMode;
      token = parsed.token;
    }
    // Stdio is always service_account — the picker isn't rendered
    // for stdio, but ``formAuthMode`` may carry a stale OAuth choice
    // from a prior HTTP entry on this same form. Force-bake here so
    // the submit shape matches the backend's invariant regardless of
    // form-state ordering.
    if (transport === "stdio") {
      authMode = "service_account";
    }
    // Apply optional overrides from the secret-detection "Move to
    // env vars" path: rewritten env / headers reference ${NAME} and
    // the buffered env-vars dict carries the extracted values.
    if (overrides?.env) env = overrides.env;
    if (overrides?.headers) headers = overrides.headers;
    const templateVarsToSubmit = overrides?.template_vars ?? formTemplateVars;

    if (!formId || !formName || (!url && !command)) {
      setFormError(t("upstreams.errorRequiredFields"));
      return;
    }

    // Defense in depth: the Next-click gate should have caught this,
    // but if features loaded after the form opened (or any other race)
    // a stdio command could reach step 2. Show the same promo dialog
    // and never surface the "disabled" error inline.
    if (command && !allowStdioMcp) {
      track("stdio_mcp_attempted", { was_blocked: true });
      setShowStdioPromo(true);
      return;
    }

    if (url) {
      try {
        new URL(url);
      } catch {
        setFormError(t("upstreams.errorInvalidUrl"));
        return;
      }
    }

    setAdding(true);
    setResourcesField(null);
    try {
      const isStdio = Boolean(command);
      const result = await addUpstream({
        id: formId,
        display_name: formName,
        url,
        headers,
        command,
        args,
        env,
        auth_mode: authMode,
        auth_token: token,
        client_id: formClientId || undefined,
        client_secret: formClientSecret || undefined,
        ...(Object.keys(templateVarsToSubmit).length > 0
          ? { template_vars: templateVarsToSubmit } : {}),
        // Resource fields are stdio-only and only sent when the user
        // had a chance to pick (i.e. capabilities loaded). The backend
        // applies model defaults for any missing field.
        ...(isStdio && capabilities && formCpu !== null && formMemory !== null
          ? {
              cpu_vcpus: formCpu,
              memory_mb: formMemory,
              ...(capabilities.allowed_disk_gb.length > 0 && formDisk !== null
                ? { disk_gb: formDisk } : {}),
              ...(capabilities.supports_persistent_disk
                ? { persistent_disk_enabled: formPersistentDisk } : {}),
            }
          : {}),
      });
      setShowForm(false);
      setUpstreams((prev) => [...prev, result]);
      setFormTemplateVars({});
    } catch (e) {
      if (maybeHandlePlanLimit(e, { source: "add_upstream_button" })) {
        setShowForm(false);
        setAdding(false);
        return;
      }
      const msg = e instanceof Error ? e.message : t("upstreams.failedToAdd");
      setFormError(msg);
      // The backend's ResourcesUnsupported path returns
      // ``{message, field, value}``; flag the offending control so
      // the user can see which dropdown is out of range.
      try {
        const parsed = JSON.parse(msg) as { field?: string };
        if (parsed.field) setResourcesField(parsed.field);
      } catch {
        // Plain-text error; leave field unset.
      }
    } finally {
      setAdding(false);
    }
  };

  /** Top-level submit: run the client-side secret scan first; if
   *  any findings AND the user hasn't dismissed for this id, show
   *  the SecretDetectionDialog and gate the create call on its
   *  resolution. Otherwise proceed straight to ``performAdd``. */
  const handleAdd = async () => {
    if (formTab === "json") {
      const parsed = parseJson(formJson);
      if (parsed) {
        const findings = scanConfigForSecrets(parsed.env, parsed.headers);
        if (
          findings.length > 0 &&
          !isSecretDetectionDismissed(formId)
        ) {
          setPendingFindings(findings);
          return;
        }
      }
    }
    await performAdd();
  };

  /** Resolve the SecretDetectionDialog. ``moved`` extractions get
   *  the value pulled out of env/headers, written into the buffered
   *  secrets dict, and the JSON value rewritten to ${NAME}.
   */
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
    if (resolution.kind === "dismissed") return;
    if (extractions.length === 0) {
      // "Keep in JSON anyway" — proceed with the original values.
      await performAdd();
      return;
    }
    // Apply each move: rewrite the JSON to ${NAME} and stash the
    // value in formTemplateVars (always as a secret since the value was
    // detected as secret-shaped). Also re-render the JSON box so
    // the user sees the ${NAME} reference before it's submitted.
    const parsed = parseJson(formJson);
    if (!parsed) return;
    const newEnv = { ...(parsed.env ?? {}) };
    const newHeaders = { ...(parsed.headers ?? {}) };
    const newTemplateVars: Record<string, BufferedTemplateVar> = { ...formTemplateVars };
    for (const { finding, envVarName, value, isSecret } of extractions) {
      const placeholder = `\${${envVarName}}`;
      if (finding.field === "env") {
        newEnv[finding.key] = newEnv[finding.key].replace(value, placeholder);
      } else {
        newHeaders[finding.key] = newHeaders[finding.key].replace(value, placeholder);
      }
      newTemplateVars[envVarName] = { value, is_secret: isSecret };
    }
    setFormTemplateVars(newTemplateVars);
    // Rewrite the visible JSON so the user can see what changed.
    try {
      const wrapped = JSON.parse(formJson);
      const target = wrapped.url || wrapped.command ? wrapped : Object.values(wrapped)[0];
      if (target && Object.keys(newEnv).length > 0) target.env = newEnv;
      if (target && Object.keys(newHeaders).length > 0) target.headers = newHeaders;
      setFormJson(JSON.stringify(wrapped, null, 2));
    } catch {
      // Non-fatal; the create call below uses the in-memory env / headers.
    }
    await performAdd({
      env: Object.keys(newEnv).length > 0 ? newEnv : undefined,
      headers: Object.keys(newHeaders).length > 0 ? newHeaders : undefined,
      template_vars: newTemplateVars,
    });
  };

  const handleRemove = async (id: string) => {
    const ok = await confirm({
      title: t("common.remove"),
      message: t("upstreams.confirmRemove", { id }),
      confirmLabel: t("common.remove"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    await removeUpstream(id);
    reload();
  };


  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div>
      {!showForm && (
      <div className="mb-4">
        <div className="flex items-center gap-3">
          <h2 className="text-xl font-semibold text-zinc-900">
            {t("upstreams.title")}
          </h2>
          {user?.current_org && (
            <UpstreamCapacityPills
              plan={user.current_org.plan}
              httpCount={httpCount}
              stdioCount={stdioCount}
            />
          )}
        </div>
        <p className="mt-1 text-sm text-zinc-500">{t("upstreams.description")}</p>
      </div>
      )}
      {!showForm && upstreams.length > 0 && (
        <div className="flex justify-end gap-2 mb-4">
          {!showForm && (
            <button
              onClick={() => setShowImport(true)}
              className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-50 flex items-center gap-1.5"
            >
              <Upload size={14} />
              {t("upstreams.import")}
            </button>
          )}
          <button
            onClick={() => {
              if (showForm) {
                setShowForm(false);
              } else {
                setFormStep(1);
                setFormTab("url");
                setFormId("");
                setFormName("");
                setFormIdTouched(false);
                setFormNameTouched(false);
                setFormUrl("");
                setFormJson("");
                setFormAuthMode("per_user_oauth");
                setFormClientId("");
                setFormClientSecret("");
                setFormAdvancedOpen(false);
                setFormError("");
                // Reset resource picker so a previous stdio attempt
                // doesn't leak its CPU/RAM choices into the next add.
                setFormCpu(null);
                setFormMemory(null);
                setFormDisk(null);
                setFormPersistentDisk(false);
                setResourcesField(null);
                setShowForm(true);
              }
            }}
            className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
          >
            {showForm ? t("common.cancel") : <><Plus size={14} /> {t("upstreams.addMcp")}</>}
          </button>
        </div>
      )}

      {showForm && (
        <div className="mb-4">
          {/* Step indicator */}
          <div className="flex items-center gap-2 mb-3 text-xs text-zinc-400">
            <span className={`font-medium ${formStep === 1 ? "text-blue-600" : "text-zinc-500"}`}>1. Configuration</span>
            <span>→</span>
            <span className={`font-medium ${formStep === 2 ? "text-blue-600" : "text-zinc-400"}`}>2. Details</span>
          </div>

          {formStep === 1 && (
            <>
              <SettingsCard title="Configuration">
                <SettingsField label="MCP server endpoint">
                  <div className="inline-flex rounded border border-zinc-300 mb-2">
                    <button
                      onClick={() => setFormTab("url")}
                      className={`px-3 py-1 text-sm ${
                        formTab === "url"
                          ? "bg-zinc-800 text-white"
                          : "text-zinc-500 hover:text-zinc-700"
                      }`}
                    >
                      {t("upstreams.tabUrl")}
                    </button>
                    <button
                      onClick={() => setFormTab("json")}
                      className={`px-3 py-1 text-sm border-l border-zinc-300 ${
                        formTab === "json"
                          ? "bg-zinc-800 text-white"
                          : "text-zinc-500 hover:text-zinc-700"
                      }`}
                    >
                      {t("upstreams.tabJson")}
                    </button>
                  </div>
                  {formTab === "url" ? (
                    <>
                      <input
                        value={formUrl}
                        onChange={(e) => setFormUrl(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") handleNext(); }}
                        className="w-full px-2 py-1.5 border border-zinc-300 rounded text-sm bg-white"
                        placeholder={t("upstreams.placeholderUrl")}
                        autoFocus
                      />
                      {formUrl && !/^https?:\/\/.+/.test(formUrl) && !/^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(\/.*)?$/.test(formUrl) && (
                        <FieldHint error>Please enter a valid URL (e.g. https://example.com/mcp)</FieldHint>
                      )}
                    </>
                  ) : (
                    <>
                      <JsonConfigEditor
                        value={formJson}
                        onChange={setFormJson}
                        rows={10}
                        placeholder={t("upstreams.placeholderJson")}
                      />
                      {/* Validation errors render above the
                          informational hint (and in red) so an
                          operator with a syntax error sees the
                          actionable message first, not buried under
                          a paragraph of help text. */}
                      {jsonParseResult === "syntax_error" && (
                        <FieldHint error>{t("upstreams.errorInvalidJson")}</FieldHint>
                      )}
                      {jsonParseResult === "missing_fields" && (
                        <FieldHint error>{t("upstreams.errorMissingUrlOrCommand")}</FieldHint>
                      )}
                      <FieldHint>
                        Use <code>{'${MY_VAR}'}</code> to reference a variable from the list below. Use <code>{'\\${VAR}'}</code> to prevent expansion.
                      </FieldHint>
                    </>
                  )}
                </SettingsField>
                {formTab === "json" && (
                  <div>
                    <TemplateVarsManager
                      upstreamId=""
                      references={(() => {
                        try {
                          return findReferences(JSON.parse(formJson));
                        } catch {
                          return [];
                        }
                      })()}
                      initialBuffered={formTemplateVars}
                      onBufferedChange={setFormTemplateVars}
                      handleRef={templateVarsManagerRef}
                      isStdio={formIsStdio}
                    />
                  </div>
                )}

                {/* Auth mode + OAuth client credentials live on step
                    2 alongside ID + Display Name. Step 1 is intentionally
                    minimal — paste the URL or the JSON (with optional
                    Variables in the JSON tab) and click Next. The
                    operator picks an auth mode after they've committed
                    to the connection shape. */}
              </SettingsCard>

              {formError && (
                <p className="mb-2 text-sm text-red-600">{formError}</p>
              )}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowForm(false)}
                  className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-100"
                >
                  {t("common.cancel")}
                </button>
                <button
                  onClick={handleNext}
                  disabled={formTab === "url"
                    ? !formUrl.trim() || (!(/^https?:\/\/.+/.test(formUrl.trim())) && !(/^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(\/.*)?$/.test(formUrl.trim())))
                    : !formJson.trim() || !unwrapJson(formJson)
                  }
                  className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded not-disabled:hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
                >
                  Next <ArrowRight size={14} />
                </button>
              </div>
            </>
          )}

          {formStep === 2 && (
            <>
              <SettingsCard title="Identity">
                <SettingsField label={t("upstreams.formId")}>
                  <IdInput
                    value={formId}
                    onChange={(id) => {
                      setFormId(id);
                      setFormIdTouched(true);
                      if (!formNameTouched) {
                        setFormName(idToDisplayName(id));
                      }
                    }}
                    placeholder={t("upstreams.placeholderId")}
                    className="w-full max-w-md"
                  />
                  {idExists && (
                    <p className="mt-1 text-xs text-red-500">{t("upstreams.errorIdExists")}</p>
                  )}
                </SettingsField>

                <SettingsField label={t("upstreams.formDisplayName")}>
                  <input
                    value={formName}
                    onChange={(e) => {
                      setFormName(e.target.value);
                      setFormNameTouched(true);
                    }}
                    className="w-full max-w-md px-2 py-1.5 border border-zinc-300 rounded text-sm bg-white"
                    placeholder={t("upstreams.placeholderName")}
                  />
                </SettingsField>

                {/* Stdio MCPs are always service_account — OAuth modes
                    require an HTTP endpoint the gateway can drive the
                    OAuth handshake against, which stdio sandboxes
                    can't host. The backend rejects stdio + OAuth at
                    write time; we just don't render the picker for
                    stdio so operators don't have a knob that can only
                    produce a 400. */}
                {!formIsStdio && (
                  <SettingsField label={t("upstreams.formAuthMode")}>
                    <div className="space-y-2 max-w-md">
                      {(["admin_oauth", "per_user_oauth", "service_account"] as const).map((mode) => (
                        <label
                          key={mode}
                          className={`flex items-start gap-2 p-2 rounded border cursor-pointer ${
                            formAuthMode === mode
                              ? "border-blue-300 bg-blue-50"
                              : "border-zinc-200 hover:border-zinc-300"
                          }`}
                        >
                          <input
                            type="radio"
                            name="authMode"
                            value={mode}
                            checked={formAuthMode === mode}
                            onChange={() => setFormAuthMode(mode)}
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
                  </SettingsField>
                )}

                {/* OAuth client credentials — collapsible, only
                    rendered when the upstream is HTTP + OAuth-shaped.
                    For stdio (always service_account) and
                    HTTP-with-service_account the fold would be empty
                    so we suppress it. */}
                {!formIsStdio && (formAuthMode === "admin_oauth" || formAuthMode === "per_user_oauth") && (
                  <div className="border border-zinc-200 rounded-lg">
                    <button
                      type="button"
                      onClick={() => setFormAdvancedOpen(!formAdvancedOpen)}
                      className="flex items-center gap-2 w-full px-3 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-100 text-left rounded-lg"
                    >
                      {formAdvancedOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      OAuth client credentials
                    </button>
                    {formAdvancedOpen && (
                      <div className="px-3 pb-3 border-t border-zinc-200 space-y-2 pt-3">
                        <p className="text-xs text-zinc-500">
                          Required for servers that don't support dynamic client registration (e.g. GitHub).
                        </p>
                        <input
                          type="text"
                          value={formClientId}
                          onChange={(e) => setFormClientId(e.target.value)}
                          placeholder="OAuth Client ID"
                          className="w-full px-2 py-1.5 border border-zinc-300 rounded text-sm bg-white"
                        />
                        <input
                          type="password"
                          value={formClientSecret}
                          onChange={(e) => setFormClientSecret(e.target.value)}
                          placeholder="OAuth Client Secret"
                          className="w-full px-2 py-1.5 border border-zinc-300 rounded text-sm bg-white"
                        />
                      </div>
                    )}
                  </div>
                )}
              </SettingsCard>

              {/* Sandbox resources (stdio only). Always rendered for
                  stdio so the user sees the section is on its way; the
                  controls swap in once capabilities load. Hidden for
                  HTTP (no sandbox). */}
              {formIsStdio && (
                <SettingsCard title="Sandbox">
                  <SettingsField label="Resources">
                    {!capabilities && !capabilitiesError && (
                      <p className="text-xs text-zinc-400">Loading available resource configurations…</p>
                    )}
                    {capabilitiesError && (
                      <p className="text-xs text-red-600">
                        Could not load resource grid: {capabilitiesError}. The MCP will use defaults.
                      </p>
                    )}
                    {capabilities && formCpu !== null && formMemory !== null && (<>
                      <SandboxComboSelect
                        capabilities={capabilities}
                        value={{
                          cpu_vcpus: formCpu,
                          memory_mb: formMemory,
                          disk_gb: formDisk ?? 0,
                        }}
                        onChange={(combo) => {
                          setFormCpu(combo.cpu_vcpus);
                          setFormMemory(combo.memory_mb);
                          setFormDisk(combo.disk_gb);
                        }}
                        hasError={!!resourcesField}
                        command={formCommand}
                      />
                      {capabilities.supports_persistent_disk && (
                        <label className="mt-3 inline-flex items-center gap-2 text-sm text-zinc-700">
                          <input
                            type="checkbox"
                            checked={formPersistentDisk}
                            onChange={(e) => setFormPersistentDisk(e.target.checked)}
                          />
                          Persistent volume
                          <span className="text-xs text-zinc-400">
                            (mount /data across sessions)
                          </span>
                        </label>
                      )}
                    </>)}
                  </SettingsField>
                </SettingsCard>
              )}

              {formError && (
                <p className="mt-2 text-sm text-red-600">{formError}</p>
              )}
              <div className="flex items-center gap-2 mt-3">
                <button
                  onClick={() => setShowForm(false)}
                  className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-100"
                >
                  {t("common.cancel")}
                </button>
                <button
                  onClick={() => { setFormStep(1); setFormError(""); }}
                  className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-100 flex items-center gap-1.5"
                >
                  <ArrowLeft size={14} /> Back
                </button>
                <button
                  onClick={handleAdd}
                  disabled={adding || idExists || !formId || !formName}
                  className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded not-disabled:hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {adding ? t("common.adding") : t("common.add")}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {upstreams.length === 0 && !showForm && (
        <div className="flex flex-col items-center justify-center py-20 space-y-6">
          <button
            onClick={() => setShowForm(true)}
            className="w-80 p-6 border-2 border-dashed border-zinc-300 rounded-xl hover:border-blue-400 hover:bg-blue-50 transition-colors text-center group"
          >
            <div className="flex items-center justify-center w-12 h-12 mx-auto mb-3 bg-blue-100 rounded-full group-hover:bg-blue-200 transition-colors">
              <Plus size={24} className="text-blue-600" />
            </div>
            <p className="text-base font-semibold text-zinc-900">{t("upstreams.addMcp")}</p>
            <p className="text-sm text-zinc-500 mt-1">{t("upstreams.emptyAddSubtitle")}</p>
          </button>
          <span className="text-sm text-zinc-400">{t("upstreams.emptyOr")}</span>
          <button
            onClick={() => setShowImport(true)}
            className="w-80 p-6 border-2 border-dashed border-zinc-300 rounded-xl hover:border-zinc-400 hover:bg-zinc-50 transition-colors text-center group"
          >
            <div className="flex items-center justify-center w-12 h-12 mx-auto mb-3 bg-zinc-100 rounded-full group-hover:bg-zinc-200 transition-colors">
              <Upload size={24} className="text-zinc-600" />
            </div>
            <p className="text-base font-semibold text-zinc-900">{t("upstreams.import")}</p>
            <p className="text-sm text-zinc-500 mt-1">{t("upstreams.emptyImportSubtitle")}</p>
          </button>
        </div>
      )}

      {upstreams.length > 0 && !showForm && <div className="border border-zinc-200 rounded-lg overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm table-fixed">
          <thead className="bg-zinc-50 text-zinc-600">
            <tr>
              <th className="text-left px-4 py-2 font-medium w-[26%]">{t("upstreams.headerName")}</th>
              <th className="text-left px-4 py-2 font-medium w-[14%]">{t("upstreams.headerTransport")}</th>
              <th className="text-left px-4 py-2 font-medium w-[16%]">{t("upstreams.headerAuthentication")}</th>
              <th className="text-left px-4 py-2 font-medium w-[18%]">{t("upstreams.headerStatus")}</th>
              <th className="text-right px-4 py-2 font-medium w-[8%]">{t("upstreams.headerTools")}</th>
              <th className="text-right px-4 py-2 font-medium w-[18%]">{t("upstreams.headerActions")}</th>
              <th className="w-10"></th>
            </tr>
          </thead>
          <tbody>
            {upstreams.map((u) => {
              return (
                <React.Fragment key={u.id}>
                <tr className="border-t border-zinc-100 hover:bg-zinc-50">
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-2">
                      <UpstreamFavicon url={u.url} size={16} />
                      <Link
                        to={`/orgs/${orgSlug}/admin/upstream/${u.id}`}
                        className="text-blue-600 hover:underline font-medium"
                      >
                        {u.display_name}
                      </Link>
                    </div>
                  </td>
                  <td className="px-4 py-2">
                    <TransportBadge transport={u.transport} />
                  </td>
                  <td className="px-4 py-2">
                    <AuthBadge authMode={u.auth_mode} />
                  </td>
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <StatusBadge
                        connected={u.ready}
                        disconnectReason={u.disconnect_reason}
                        starting={u.starting}
                        label={upstreamStatusLabel(u, user?.email ?? null, t)}
                      />
                      {u.refreshing && (
                        <span
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
                          title={t("upstreams.fetchingInfoTooltip")}
                        >
                          <Loader2 size={10} className="animate-spin shrink-0" />
                          <span className="truncate min-w-0">
                            {t("upstreams.fetchingInfo")}
                          </span>
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-600">
                    {u.ready ? (
                      u.tool_count > 0 ? (
                        <Link
                          to={`/orgs/${orgSlug}/admin/upstream/${u.id}`}
                          className="inline-flex items-center gap-0.5 text-blue-500 hover:text-blue-700 hover:underline cursor-pointer text-sm font-medium"
                        >
                          {u.tool_count}
                          <ChevronRight size={12} />
                        </Link>
                      ) : (
                        u.tool_count
                      )
                    ) : (
                      t("upstreams.toolsUnknown")
                    )}
                  </td>
                  <td className="px-4 py-2 text-right space-x-2">
                    <UpstreamActionButtons
                      id={u.id}
                      ready={u.ready}
                      transport={u.transport}
                      authMode={u.auth_mode}
                      starting={u.starting}
                      reload={reload}
                    />
                  </td>
                  <td className="px-2 py-2 text-center">
                    <Tooltip>
                      <TooltipTrigger
                        onClick={() => handleRemove(u.id)}
                        className="p-1 text-zinc-300 hover:text-red-500 rounded"
                      >
                        <Trash2 size={14} />
                      </TooltipTrigger>
                      <TooltipContent>{t("common.remove")}</TooltipContent>
                    </Tooltip>
                  </td>
                </tr>
                {!u.ready && u.disconnect_reason && (() => {
                  const hasKey = `disconnectReason.${u.disconnect_reason}` in en;
                  return (
                    <tr key={`${u.id}-error`}>
                      <td colSpan={7} className="px-4 pt-0 pb-2">
                        <div className={`text-xs px-3 py-1.5 rounded ${
                          hasKey
                            ? "bg-amber-50 text-amber-700"
                            : "bg-red-50 text-red-600"
                        }`}>
                          {hasKey
                            ? t(`disconnectReason.${u.disconnect_reason}` as TranslationKey)
                            : u.disconnect_reason}
                        </div>
                      </td>
                    </tr>
                  );
                })()}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>}
      <ConfirmDialog {...dialogProps} />
      <StdioPromoDialog
        open={showStdioPromo}
        onClose={() => setShowStdioPromo(false)}
        notifyEmail={user?.email ?? null}
      />
      <ImportMcpModal
        open={showImport}
        onClose={() => setShowImport(false)}
        onComplete={() => { setShowImport(false); invalidate(); }}
      />
      <SecretDetectionDialog
        open={pendingFindings.length > 0}
        findings={pendingFindings}
        upstreamId={formId}
        existingNames={new Set(Object.keys(formTemplateVars))}
        onResolve={(extractions, resolution) => {
          void handleDetectionResolved(extractions, resolution);
        }}
        resolveValue={(finding) => {
          // Look up the original (un-truncated) value in the parsed JSON.
          const parsed = parseJson(formJson);
          if (!parsed) return finding.matchPreview;
          const source =
            finding.field === "env" ? parsed.env : parsed.headers;
          return source?.[finding.key] ?? finding.matchPreview;
        }}
      />
    </div>
  );
}
