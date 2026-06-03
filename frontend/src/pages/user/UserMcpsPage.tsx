import { useEffect, useState } from "react";
import { fetchUserMcps, connectUpstream, disconnectUpstream } from "../../api/user";
import { track } from "../../lib/analytics";
import type { UserMcpInfo } from "../../api/types";
import { useTranslation } from "../../i18n/index";
import { useOAuthPopup } from "../../hooks/useOAuthPopup";
import { UpstreamFavicon } from "../../components/UpstreamFavicon";
import { ChevronDown, ChevronRight, AlertTriangle, LogIn, CircleDashed } from "lucide-react";

type McpStatus = "ready" | "signed_in" | "sign_in_required" | "sign_in_expired" | "unavailable";

function getMcpStatus(mcp: UserMcpInfo): McpStatus {
  // Org-level not Ready — for OAuth modes this means no admin has
  // authenticated; for service_account it means the shared session
  // is down.
  if (!mcp.ready) return "unavailable";
  // Per-user OAuth: even when Ready, the viewer's own row drives
  // the per-card "signed in?" pill.
  if (mcp.auth_mode === "per_user_oauth") {
    if (mcp.user_connection_status === "connected") return "signed_in";
    if (mcp.user_connection_status === "reconnect_needed") return "sign_in_expired";
    return "sign_in_required";
  }
  // service_account or admin_oauth — Ready ⇒ usable for the viewer.
  return "ready";
}

function StatusIndicator({ status }: { status: McpStatus }) {
  const { t } = useTranslation();

  const config: Record<McpStatus, { label: string; className: string; dotClass: string }> = {
    ready: {
      label: t("userMcps.statusReady"),
      className: "text-green-700 bg-green-50",
      dotClass: "bg-green-500",
    },
    signed_in: {
      label: t("userMcps.statusSignedIn"),
      className: "text-green-700 bg-green-50",
      dotClass: "bg-green-500",
    },
    sign_in_required: {
      label: t("userMcps.statusSignInRequired"),
      className: "text-amber-700 bg-amber-50",
      dotClass: "bg-amber-500",
    },
    sign_in_expired: {
      label: t("userMcps.statusSignInExpired"),
      className: "text-amber-700 bg-amber-50",
      dotClass: "bg-amber-500",
    },
    unavailable: {
      label: t("userMcps.statusUnavailable"),
      className: "text-zinc-500 bg-zinc-50",
      dotClass: "bg-zinc-400",
    },
  };

  const c = config[status];
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${c.className}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${c.dotClass}`} />
      {c.label}
    </span>
  );
}

function ToolCountLabel({ mcp }: { mcp: UserMcpInfo }) {
  const { t } = useTranslation();
  if (mcp.tool_count === 0) return <span className="text-zinc-400 text-sm">{t("userMcps.noTools")}</span>;
  const label = mcp.tool_count === 1 ? t("userMcps.toolCountOne") : t("userMcps.toolCount", { count: mcp.tool_count });
  return <span className="text-zinc-500 text-sm">{label}</span>;
}

function ToolsList({ mcp }: { mcp: UserMcpInfo }) {
  const [expanded, setExpanded] = useState(false);
  const { t } = useTranslation();

  if (mcp.tool_count === 0) return null;

  return (
    <div>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-700 transition-colors"
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {expanded
          ? t("userMcps.hideTools")
          : t("userMcps.showTools") + ` (${mcp.tool_count})`}
      </button>
      {expanded && (
        <div className="mt-2 space-y-1 pl-5">
          {mcp.tools.map((tool) => (
            <div key={tool.name} className="text-sm">
              <span className="font-medium text-zinc-700">{tool.name}</span>
              {tool.description && (
                <span className="text-zinc-400 ml-2">{tool.description.split(/\.(?:\s|$)/)[0]}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ReadyCard({ mcp }: { mcp: UserMcpInfo }) {
  const status = getMcpStatus(mcp);
  return (
    <div className="border border-zinc-200 rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <UpstreamFavicon url={mcp.url} size={20} />
          <h3 className="font-medium text-zinc-900">{mcp.display_name}</h3>
        </div>
        <StatusIndicator status={status} />
      </div>
      <ToolsList mcp={mcp} />
    </div>
  );
}

function SignedInCard({
  mcp,
  onDisconnect,
  actionLoading,
}: {
  mcp: UserMcpInfo;
  onDisconnect: () => void;
  actionLoading: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="border border-zinc-200 rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <UpstreamFavicon url={mcp.url} size={20} />
          <h3 className="font-medium text-zinc-900">{mcp.display_name}</h3>
        </div>
        <StatusIndicator status="signed_in" />
      </div>
      <div className="flex items-center justify-between">
        <ToolsList mcp={mcp} />
        <button
          onClick={onDisconnect}
          disabled={actionLoading}
          className="text-xs text-zinc-400 hover:text-red-600 disabled:opacity-50 transition-colors"
        >
          {actionLoading ? t("userMcps.disconnecting") : t("userMcps.disconnect")}
        </button>
      </div>
    </div>
  );
}

function ActionNeededCard({
  mcp,
  onConnect,
  actionLoading,
}: {
  mcp: UserMcpInfo;
  onConnect: () => void;
  actionLoading: boolean;
}) {
  const { t } = useTranslation();
  const status = getMcpStatus(mcp);
  return (
    <div className="border border-amber-200 rounded-lg p-4 space-y-3 bg-amber-50/30">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <UpstreamFavicon url={mcp.url} size={20} />
          <h3 className="font-medium text-zinc-900">{mcp.display_name}</h3>
        </div>
        <StatusIndicator status={status} />
      </div>
      <p className="text-sm text-zinc-600">
        {t("userMcps.signInPrompt", { name: mcp.display_name })}
      </p>
      <div className="flex items-center justify-between">
        <ToolCountLabel mcp={mcp} />
        <button
          onClick={onConnect}
          disabled={actionLoading}
          className="inline-flex items-center gap-2 text-sm px-4 py-1.5 bg-zinc-900 text-white rounded-md hover:bg-zinc-800 disabled:opacity-50 transition-colors"
        >
          {actionLoading ? (
            <>
              <CircleDashed size={14} className="animate-spin" />
              {t("userMcps.connecting")}
            </>
          ) : (
            <>
              <LogIn size={14} />
              {t("userMcps.signIn")}
            </>
          )}
        </button>
      </div>
    </div>
  );
}

function UnavailableCard({ mcp }: { mcp: UserMcpInfo }) {
  const { t } = useTranslation();
  return (
    <div className="border border-zinc-100 rounded-lg p-4 opacity-60">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <UpstreamFavicon url={mcp.url} size={20} />
          <h3 className="font-medium text-zinc-400">{mcp.display_name}</h3>
        </div>
        <StatusIndicator status="unavailable" />
      </div>
      <p className="text-xs text-zinc-400 mt-1">{t("userMcps.unavailableHint")}</p>
    </div>
  );
}

export function UserMcpsPage() {
  const { t } = useTranslation();
  const [mcps, setMcps] = useState<UserMcpInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Capture the initial group for each MCP on first load so that
  // the order stays stable when a user connects/disconnects.
  const [pinnedTop, setPinnedTop] = useState<Set<string>>(new Set());

  const refresh = () => {
    fetchUserMcps()
      .then(setMcps)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchUserMcps()
      .then((data) => {
        setMcps(data);
        // Pin MCPs that need action on the initial load
        const top = new Set<string>();
        for (const mcp of data) {
          const status = getMcpStatus(mcp);
          if (status === "sign_in_required" || status === "sign_in_expired") {
            top.add(mcp.id);
          }
        }
        setPinnedTop(top);
      })
      .finally(() => setLoading(false));
  }, []);

  const { startOAuthFlow } = useOAuthPopup({
    onSuccess: () => {
      setActionLoading(null);
      refresh();
    },
    onError: (msg) => {
      setError(msg);
      setActionLoading(null);
    },
  });

  const handleConnect = async (upstreamId: string) => {
    track("upstream_connect_clicked", { upstream_id: upstreamId });
    setActionLoading(upstreamId);
    setError(null);
    try {
      const result = await connectUpstream(upstreamId);
      if (!startOAuthFlow(upstreamId, result, () => connectUpstream(upstreamId))) return;
      if (result.connected) {
        refresh();
      } else if (result.error) {
        setError(result.error);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("userMcps.connectionFailed"));
    }
    setActionLoading(null);
  };

  const handleDisconnect = async (upstreamId: string) => {
    setActionLoading(upstreamId);
    setError(null);
    try {
      await disconnectUpstream(upstreamId);
      // Pin the just-disconnected MCP so its Connect button shows immediately
      // (without this, it would fall into the "ready" group and render as ReadyCard).
      setPinnedTop((prev) => {
        const next = new Set(prev);
        next.add(upstreamId);
        return next;
      });
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("userMcps.disconnectFailed"));
    } finally {
      setActionLoading(null);
    }
  };

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  // Group MCPs by status
  const actionNeeded: UserMcpInfo[] = [];
  const ready: UserMcpInfo[] = [];
  const unavailable: UserMcpInfo[] = [];

  for (const mcp of mcps) {
    const status = getMcpStatus(mcp);
    if (status === "unavailable") {
      unavailable.push(mcp);
    } else if (pinnedTop.has(mcp.id)) {
      actionNeeded.push(mcp);
    } else {
      ready.push(mcp);
    }
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-xl font-semibold text-zinc-900">{t("userMcps.title")}</h2>
        <p className="text-sm text-zinc-500 mt-1">{t("userMcps.subtitle")}</p>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 text-red-700 text-sm rounded border border-red-200 flex items-center gap-2">
          <AlertTriangle size={16} />
          {error}
        </div>
      )}

      {mcps.length === 0 ? (
        <p className="text-zinc-400 text-sm">{t("userMcps.noMcps")}</p>
      ) : (
        <div className="space-y-3">
          {actionNeeded.map((mcp) => {
            const status = getMcpStatus(mcp);
            return status === "signed_in" ? (
              <SignedInCard
                key={mcp.id}
                mcp={mcp}
                onDisconnect={() => handleDisconnect(mcp.id)}
                actionLoading={actionLoading === mcp.id}
              />
            ) : (
              <ActionNeededCard
                key={mcp.id}
                mcp={mcp}
                onConnect={() => handleConnect(mcp.id)}
                actionLoading={actionLoading === mcp.id}
              />
            );
          })}
          {ready.map((mcp) => {
            const status = getMcpStatus(mcp);
            return status === "signed_in" ? (
              <SignedInCard
                key={mcp.id}
                mcp={mcp}
                onDisconnect={() => handleDisconnect(mcp.id)}
                actionLoading={actionLoading === mcp.id}
              />
            ) : (
              <ReadyCard key={mcp.id} mcp={mcp} />
            );
          })}
          {unavailable.map((mcp) => (
            <UnavailableCard key={mcp.id} mcp={mcp} />
          ))}
        </div>
      )}
    </div>
  );
}
