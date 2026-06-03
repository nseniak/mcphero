import { useEffect, useState } from "react";
import { Copy, Check } from "lucide-react";
import { fetchGatewayConfig } from "../../api/user";
import { fetchAdminMcpTools, type AdminMcpToolCategory } from "../../api/admin";
import { useTranslation } from "../../i18n/index";
import { ToolTable } from "../../components/ToolTable";

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

function adminMcpUrl(gatewayUrl: string): string {
  // /mcp → /admin-mcp, /mcp/acme-corp → /admin-mcp/acme-corp
  return gatewayUrl.replace(/\/mcp(\/|$)/, "/admin-mcp$1");
}

function mcpJsonConfig(url: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        "mcphero-admin": { url },
      },
    },
    null,
    2,
  );
}

export function AdminMcpPage() {
  const { t } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [categories, setCategories] = useState<AdminMcpToolCategory[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const gw = await fetchGatewayConfig();
        setUrl(adminMcpUrl(gw.url));
      } catch {
        // gateway config unavailable
      }
      try {
        const result = await fetchAdminMcpTools();
        setCategories(result);
      } catch {
        // admin tools unavailable
      }
      setLoading(false);
    };
    load();
  }, []);

  const totalTools = categories.reduce((sum, c) => sum + c.tools.length, 0);

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-zinc-900">{t("adminMcp.title")}</h2>
        <p className="mt-1 text-sm text-zinc-500">{t("adminMcp.description")}</p>
      </div>

      {url && (
        <div className="space-y-4">
          {/* Admin MCP URL */}
          <div>
            <label className="block text-sm font-medium text-zinc-700 mb-1">
              {t("adminMcp.urlLabel")}
            </label>
            <div className="relative inline-flex items-center max-w-xl w-full">
              <code className="w-full px-3 py-2 pr-16 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900 truncate">
                {url}
              </code>
              <div className="absolute right-2">
                <CopyButton text={url} />
              </div>
            </div>
          </div>

          {/* JSON config */}
          <div className="max-w-xl">
            <label className="block text-sm font-medium text-zinc-700 mb-1">
              {t("adminMcp.configTitle")}
            </label>
            <div className="relative">
              <pre className="px-3 py-2 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900 overflow-x-auto">
                {mcpJsonConfig(url)}
              </pre>
              <div className="absolute top-2 right-2">
                <CopyButton text={mcpJsonConfig(url)} />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Available tools by category */}
      <h3 className="text-lg font-medium text-zinc-900">
        {t("adminMcp.availableTools")} ({totalTools})
      </h3>
      {categories.map((cat) => (
        <div key={cat.category}>
          <h4 className="text-sm font-medium text-zinc-700 mb-2">
            {cat.category}
          </h4>
          <ToolTable tools={cat.tools} />
        </div>
      ))}
    </div>
  );
}
