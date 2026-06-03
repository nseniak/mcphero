import { useEffect, useState } from "react";
import { Copy, Check, ChevronDown, ChevronRight, Loader2, Unplug } from "lucide-react";
import { fetchGatewayConfig } from "../../api/user";
import { disconnectGatewayUser } from "../../api/admin";
import { ActionButton } from "../../components/ui/action-button";
import { useTranslation } from "../../i18n/index";

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

function mcpJsonSnippet(url: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        "mcphero": { url },
      },
    },
    null,
    2,
  );
}

export function GatewayPage() {
  const { t } = useTranslation();
  const [gatewayUrl, setGatewayUrl] = useState<string | null>(null);
  const [connectedUsers, setConnectedUsers] = useState<string[]>([]);
  const [allUsers, setAllUsers] = useState<string[]>([]);
  const [configOpen, setConfigOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);

  const reload = () => {
    fetchGatewayConfig().then((gw) => {
      setGatewayUrl(gw.url);
      setConnectedUsers(gw.connected_users);
      setAllUsers(gw.all_users);
    });
  };

  useEffect(() => {
    fetchGatewayConfig()
      .then((gw) => {
        setGatewayUrl(gw.url);
        setConnectedUsers(gw.connected_users);
        setAllUsers(gw.all_users);
      })
      .finally(() => setLoading(false));
  }, []);

  const handleDisconnect = async (email: string) => {
    setDisconnecting(email);
    try {
      await disconnectGatewayUser(email);
      reload();
    } finally {
      setDisconnecting(null);
    }
  };

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-900">{t("gateway.title")}</h2>
        <p className="mt-1 text-sm text-zinc-500">{t("gateway.description")}</p>
      </div>

      {/* Gateway URL */}
      {gatewayUrl && (
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-zinc-700 mb-1">
              {t("gateway.urlLabel")}
            </label>
            <div className="flex items-center gap-2">
              <code className="flex-1 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900">
                {gatewayUrl}
              </code>
              <CopyButton text={gatewayUrl} />
            </div>
          </div>

          {/* Collapsible JSON config */}
          <div className="border border-zinc-200 rounded">
            <button
              onClick={() => setConfigOpen(!configOpen)}
              className="flex items-center gap-2 w-full px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 transition-colors"
            >
              {configOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
              {t("gateway.configTitle")}
            </button>
            {configOpen && (
              <div className="border-t border-zinc-200 p-3">
                <div className="flex justify-end mb-1">
                  <CopyButton text={mcpJsonSnippet(gatewayUrl)} />
                </div>
                <pre className="text-xs font-mono bg-zinc-50 p-3 rounded overflow-x-auto">
                  {mcpJsonSnippet(gatewayUrl)}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Users table */}
      <div>
        <h3 className="text-sm font-medium text-zinc-700 mb-2">
          {t("gateway.connectedUsersTitle")}
        </h3>
        {allUsers.length === 0 ? (
          <p className="text-sm text-zinc-400">{t("gateway.noConnectedUsers")}</p>
        ) : (
          <div className="border border-zinc-200 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-50 text-zinc-600">
                <tr>
                  <th className="text-left px-4 py-2 font-medium">{t("gateway.userColumn")}</th>
                  <th className="text-left px-4 py-2 font-medium">{t("gateway.connectionColumn")}</th>
                  <th className="text-right px-4 py-2 font-medium">{t("gateway.actionColumn")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {allUsers.map((email) => {
                  const isConnected = connectedUsers.includes(email);
                  return (
                    <tr key={email} className="hover:bg-zinc-50">
                      <td className="px-4 py-2 text-zinc-900">{email}</td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${
                            isConnected
                              ? "bg-green-100 text-green-700"
                              : "bg-zinc-100 text-zinc-500"
                          }`}
                        >
                          <span
                            className={`w-1.5 h-1.5 rounded-full ${
                              isConnected ? "bg-green-500" : "bg-zinc-400"
                            }`}
                          />
                          {isConnected ? t("gateway.connected") : t("gateway.disconnected")}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right">
                        {isConnected && (
                          <ActionButton
                            variant="warning"
                            onClick={() => handleDisconnect(email)}
                            disabled={disconnecting === email}
                          >
                            {disconnecting === email
                              ? <Loader2 size={12} className="animate-spin" />
                              : <Unplug size={12} />}
                            {disconnecting === email
                              ? t("gateway.disconnecting")
                              : t("gateway.disconnect")}
                          </ActionButton>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
