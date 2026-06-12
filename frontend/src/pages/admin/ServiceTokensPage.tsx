import { useEffect, useState } from "react";
import { Check, Copy, KeyRound, Plus, Trash2, TriangleAlert } from "lucide-react";
import {
  createServiceToken,
  fetchRoles,
  listServiceTokens,
  revokeServiceToken,
  type ServiceTokenInfo,
} from "../../api/admin";
import { fetchGatewayConfig } from "../../api/user";
import type { RoleSummary } from "../../api/types";
import { useTranslation } from "../../i18n/index";
import { ConfirmDialog, useConfirm } from "../../components/ConfirmDialog";
import { Tooltip, TooltipTrigger, TooltipContent } from "../../components/ui/tooltip";
import { RoleBadge } from "../../components/RoleBadge";

const LABEL_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-zinc-100 hover:bg-zinc-200 rounded transition-colors"
    >
      {copied ? (
        <>
          <Check size={12} />
          {t("gateway.copied")}
        </>
      ) : (
        <>
          <Copy size={12} />
          {t("gateway.copy")}
        </>
      )}
    </button>
  );
}

function agentMcpJsonSnippet(url: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        mcphero: {
          url,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString();
}

/** Shown-once panel after a successful mint. */
function CreatedTokenPanel({
  token,
  label,
  gatewayUrl,
  onDone,
}: {
  token: string;
  label: string;
  gatewayUrl: string | null;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="border border-green-200 bg-green-50/40 rounded-lg p-4 mb-4 space-y-3">
      <div className="flex items-center gap-2">
        <KeyRound size={16} className="text-green-700" />
        <h3 className="text-sm font-semibold text-zinc-900">
          {t("serviceTokens.createdTitle")}
        </h3>
      </div>
      <div
        role="alert"
        className="rounded border border-amber-300 bg-amber-50 p-3 flex items-start gap-2"
      >
        <TriangleAlert size={16} className="text-amber-600 shrink-0 mt-0.5" />
        <p className="text-sm font-medium text-amber-900">
          {t("serviceTokens.shownOnce")}
        </p>
      </div>
      <div>
        <label className="block text-sm font-medium text-zinc-700 mb-1">
          {t("serviceTokens.tokenValue")}
        </label>
        <div className="flex items-center gap-2">
          <code
            data-testid="service-token-value"
            className="flex-1 px-3 py-2 bg-white border border-zinc-200 rounded text-sm font-mono text-zinc-900 break-all"
          >
            {token}
          </code>
          <CopyButton text={token} />
        </div>
      </div>
      {gatewayUrl && (
        <div>
          <label className="block text-sm font-medium text-zinc-700 mb-1">
            {t("serviceTokens.snippetTitle")}
          </label>
          <div className="relative">
            <div className="absolute top-2 right-2">
              <CopyButton text={agentMcpJsonSnippet(gatewayUrl, token)} />
            </div>
            <pre className="text-xs font-mono bg-zinc-50 border border-zinc-200 p-3 rounded overflow-x-auto">
              {agentMcpJsonSnippet(gatewayUrl, token)}
            </pre>
          </div>
        </div>
      )}
      <p className="text-xs text-zinc-500">
        {t("serviceTokens.auditNote", { label })}
      </p>
      <button
        onClick={onDone}
        className="px-3 py-1.5 bg-zinc-900 text-white text-sm rounded hover:bg-zinc-800"
      >
        {t("serviceTokens.done")}
      </button>
    </div>
  );
}

export function ServiceTokensPage() {
  const { t } = useTranslation();
  const { confirm, dialogProps } = useConfirm();
  const [tokens, setTokens] = useState<ServiceTokenInfo[]>([]);
  const [roles, setRoles] = useState<RoleSummary[]>([]);
  const [gatewayUrl, setGatewayUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [created, setCreated] = useState<{ token: string; label: string } | null>(
    null,
  );

  // Form state
  const [formLabel, setFormLabel] = useState("");
  const [formRole, setFormRole] = useState("");
  const [formError, setFormError] = useState("");

  const reload = () => {
    Promise.all([listServiceTokens(), fetchRoles()])
      .then(([tk, r]) => {
        setTokens(tk);
        setRoles(r);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    reload();
    fetchGatewayConfig()
      .then((gw) => setGatewayUrl(gw.url))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const isLabelValid = (label: string): boolean => LABEL_PATTERN.test(label);

  const handleCreate = async () => {
    setFormError("");
    try {
      const resp = await createServiceToken(formLabel, formRole);
      setShowForm(false);
      setFormLabel("");
      setFormRole("");
      setCreated({ token: resp.token, label: resp.info.label });
      reload();
    } catch (e) {
      setFormError(
        e instanceof Error ? e.message : t("serviceTokens.failedToCreate"),
      );
    }
  };

  const handleRevoke = async (label: string) => {
    const ok = await confirm({
      title: t("serviceTokens.revoke"),
      message: t("serviceTokens.confirmRevoke", { label }),
      confirmLabel: t("serviceTokens.revoke"),
      cancelLabel: t("common.cancel"),
      destructive: true,
    });
    if (!ok) return;
    await revokeServiceToken(label);
    if (created?.label === label) setCreated(null);
    reload();
  };

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-xl font-semibold text-zinc-900">
          {tokens.length > 0
            ? t("serviceTokens.titleCount", { count: tokens.length })
            : t("serviceTokens.title")}
        </h2>
        <p className="mt-1 text-sm text-zinc-500">
          {t("serviceTokens.description")}
        </p>
      </div>

      {created && (
        <CreatedTokenPanel
          token={created.token}
          label={created.label}
          gatewayUrl={gatewayUrl}
          onDone={() => setCreated(null)}
        />
      )}

      {!showForm && (
        <div className="flex justify-end gap-2 mb-4">
          <button
            onClick={() => {
              setCreated(null);
              setShowForm(true);
            }}
            className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 flex items-center gap-1.5"
          >
            <Plus size={14} /> {t("serviceTokens.createButton")}
          </button>
        </div>
      )}

      {showForm && (
        <div className="border border-zinc-200 rounded-lg p-4 mb-4 bg-zinc-50">
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                {t("serviceTokens.formLabel")}
              </label>
              <input
                value={formLabel}
                onChange={(e) => setFormLabel(e.target.value)}
                className={`w-full px-3 py-2 border rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent ${
                  formLabel && !isLabelValid(formLabel)
                    ? "border-red-300"
                    : "border-zinc-300"
                }`}
                placeholder={t("serviceTokens.placeholderLabel")}
              />
              {formLabel && !isLabelValid(formLabel) && (
                <p className="mt-1 text-xs text-red-500">
                  {t("serviceTokens.invalidLabel")}
                </p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-zinc-700 mb-1">
                {t("serviceTokens.formRole")}
              </label>
              <select
                value={formRole}
                onChange={(e) => setFormRole(e.target.value)}
                className="w-48 px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-zinc-900 focus:border-transparent"
              >
                <option value="">{t("serviceTokens.selectRole")}</option>
                {roles.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}
                    {r.is_admin ? ` (${t("auth.adminBadge").toLowerCase()})` : ""}
                  </option>
                ))}
              </select>
            </div>
            {formError && <p className="text-sm text-red-600">{formError}</p>}
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowForm(false)}
                className="px-3 py-1.5 border border-zinc-300 text-zinc-700 text-sm rounded hover:bg-zinc-100"
              >
                {t("common.cancel")}
              </button>
              <button
                onClick={handleCreate}
                disabled={!formLabel || !isLabelValid(formLabel) || !formRole}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {t("common.add")}
              </button>
            </div>
          </div>
        </div>
      )}

      {!showForm &&
        (tokens.length === 0 ? (
          <p className="text-zinc-400 text-sm">{t("serviceTokens.noTokens")}</p>
        ) : (
          <div className="border border-zinc-200 rounded-lg overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm table-fixed">
              <thead className="bg-zinc-50 text-zinc-600">
                <tr>
                  <th className="text-left px-4 py-2 font-medium w-[30%]">
                    {t("serviceTokens.headerLabel")}
                  </th>
                  <th className="text-left px-4 py-2 font-medium w-[20%]">
                    {t("serviceTokens.headerRole")}
                  </th>
                  <th className="text-left px-4 py-2 font-medium w-[20%]">
                    {t("serviceTokens.headerCreated")}
                  </th>
                  <th className="text-left px-4 py-2 font-medium w-[20%]">
                    {t("serviceTokens.headerLastUsed")}
                  </th>
                  <th className="w-10"></th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((tk) => (
                  <tr
                    key={tk.label}
                    className="border-t border-zinc-100 hover:bg-zinc-50"
                  >
                    <td className="px-4 py-2 font-mono text-zinc-900">
                      {tk.label}
                    </td>
                    <td className="px-4 py-2">
                      <RoleBadge role={tk.role} isAdmin={false} />
                    </td>
                    <td className="px-4 py-2 text-zinc-600">
                      {formatDate(tk.created_at)}
                    </td>
                    <td className="px-4 py-2 text-zinc-600">
                      {tk.last_used_at
                        ? formatDate(tk.last_used_at)
                        : t("serviceTokens.neverUsed")}
                    </td>
                    <td className="px-2 py-2 text-right">
                      <Tooltip>
                        <TooltipTrigger
                          onClick={() => handleRevoke(tk.label)}
                          className="p-1 text-zinc-300 hover:text-red-500 rounded"
                        >
                          <Trash2 size={14} />
                        </TooltipTrigger>
                        <TooltipContent>
                          {t("serviceTokens.revoke")}
                        </TooltipContent>
                      </Tooltip>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      <ConfirmDialog {...dialogProps} />
    </div>
  );
}
