import { useEffect, useMemo, useRef, useState } from "react";
import { Copy, Check } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchGatewayConfig } from "../../api/user";
import { useAuth } from "../../hooks/useAuth";
import { useEventSource } from "../../hooks/useEventSource";
import { useTranslation, type TranslationKey } from "../../i18n/index";

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

function WhyPill({
  label,
  explanation,
}: {
  label: string;
  explanation: string;
}) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleMouseDown = (e: MouseEvent) => {
      if (wrapperRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <span ref={wrapperRef} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-block px-1.5 ml-1 text-xs leading-tight align-middle bg-zinc-100 hover:bg-zinc-200 rounded-full text-zinc-700 cursor-pointer"
      >
        {label}
      </button>
      {open && (
        <span className="absolute left-0 top-full mt-1 z-10 w-72 p-2 text-xs font-normal bg-white border border-zinc-200 rounded shadow-lg text-zinc-700 normal-case">
          {explanation}
        </span>
      )}
    </span>
  );
}

function CopyableCode({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <>
      <code className="break-all">{text}</code>
      <button
        type="button"
        onClick={handleCopy}
        aria-label="Copy to clipboard"
        className="inline-flex items-center align-text-bottom ml-1 text-zinc-400 hover:text-zinc-700 cursor-pointer"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </>
  );
}

function CopyableBlock({ text }: { text: string }) {
  return (
    <div className="not-prose relative my-2">
      <pre className="px-3 py-2 pr-16 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900 overflow-x-auto whitespace-pre">
        {text}
      </pre>
      <div className="absolute top-2 right-2">
        <CopyButton text={text} />
      </div>
    </div>
  );
}

function mcpJsonConfig(url: string): string {
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

const CLIENT_TABS: { labelKey: TranslationKey; stepsKey: TranslationKey }[] = [
  { labelKey: "connect.claudeCode", stepsKey: "connect.claudeCodeSteps" },
  { labelKey: "connect.claudeAi", stepsKey: "connect.claudeAiSteps" },
  { labelKey: "connect.claudeDesktop", stepsKey: "connect.claudeDesktopSteps" },
  { labelKey: "connect.codex", stepsKey: "connect.codexSteps" },
  { labelKey: "connect.chatGpt", stepsKey: "connect.chatGptSteps" },
  {
    labelKey: "connect.chatGptDesktop",
    stepsKey: "connect.chatGptDesktopSteps",
  },
  { labelKey: "connect.cursor", stepsKey: "connect.cursorSteps" },
];

export function ConnectPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [gatewayUrl, setGatewayUrl] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState(0);

  useEffect(() => {
    fetchGatewayConfig()
      .then((gw) => {
        setGatewayUrl(gw.url);
        setIsConnected(
          user ? gw.connected_users.includes(user.email) : false,
        );
      })
      .finally(() => setLoading(false));
  }, [user]);

  const handlers = useMemo(
    () => ({
      gateway_connected: () => setIsConnected(true),
      gateway_disconnected: () => setIsConnected(false),
    }),
    [],
  );
  useEventSource(handlers);

  if (loading) return <p className="text-zinc-500">{t("common.loading")}</p>;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <h2 className="text-xl font-semibold text-zinc-900">{t("connect.title")}</h2>
        {isConnected !== null && (
          <span
            className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full ${
              isConnected
                ? "bg-green-100 text-green-800"
                : "bg-zinc-100 text-zinc-500"
            }`}
          >
            {isConnected ? t("connect.connected") : t("connect.disconnected")}
          </span>
        )}
      </div>
      <div>
        <p className="text-sm text-zinc-500">{t("connect.description")}</p>
      </div>

      {gatewayUrl && (
        <>
          {/* Gateway URL */}
          <div>
            <label className="block text-sm font-medium text-zinc-700 mb-1">
              {t("connect.gatewayUrl")}
            </label>
            <div className="relative inline-flex items-center max-w-xl w-full">
              <code className="w-full px-3 py-2 pr-16 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900 truncate">
                {gatewayUrl}
              </code>
              <div className="absolute right-2">
                <CopyButton text={gatewayUrl} />
              </div>
            </div>
          </div>

          {/* Client instructions */}
          <div className="max-w-3xl">
            <h3 className="text-sm font-medium text-zinc-700 mb-2">
              {t("connect.setupInstructions")}
            </h3>
            <div className="border border-zinc-200 rounded">
              {/* Tabs */}
              <div className="flex border-b border-zinc-200 overflow-x-auto">
                {CLIENT_TABS.map((tab, i) => (
                  <button
                    key={tab.labelKey}
                    onClick={() => setActiveTab(i)}
                    className={`px-4 py-2 text-sm whitespace-nowrap transition-colors ${
                      activeTab === i
                        ? "border-b-2 border-zinc-900 text-zinc-900 font-medium"
                        : "text-zinc-500 hover:text-zinc-700"
                    }`}
                  >
                    {t(tab.labelKey)}
                  </button>
                ))}
              </div>
              {/* Tab content */}
              <div className="p-4">
                <div className="prose prose-sm prose-zinc max-w-none prose-a:text-blue-600 hover:prose-a:underline prose-a:wrap-break-word prose-ol:my-0 prose-li:my-1 prose-code:before:hidden prose-code:after:hidden prose-code:bg-zinc-100 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:font-normal">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    urlTransform={(url) => url}
                    components={{
                      pre: ({ children }) => <>{children}</>,
                      code: ({ children, ...props }) => {
                        const className = (props as { className?: string })
                          .className;
                        if (className?.includes("language-")) {
                          return (
                            <CopyableBlock
                              text={String(children).replace(/\n$/, "")}
                            />
                          );
                        }
                        return <CopyableCode text={String(children)} />;
                      },
                      a: ({ href, children, ...props }) => {
                        if (href?.startsWith("why:")) {
                          const key = href.slice(4);
                          const explanationKey =
                            key === "developer-mode"
                              ? "connect.chatGptDeveloperModeWhy"
                              : null;
                          const explanation = explanationKey
                            ? t(explanationKey)
                            : "";
                          return (
                            <WhyPill
                              label={String(children)}
                              explanation={explanation}
                            />
                          );
                        }
                        if (href?.startsWith("tab:")) {
                          const tabKey = `connect.${href.slice(4)}`;
                          const tabIndex = CLIENT_TABS.findIndex(
                            (tab) => tab.labelKey === tabKey,
                          );
                          return (
                            <button
                              type="button"
                              onClick={() => {
                                if (tabIndex >= 0) setActiveTab(tabIndex);
                              }}
                              className="text-blue-600 hover:underline cursor-pointer bg-transparent border-0 p-0 font-inherit"
                            >
                              {children}
                            </button>
                          );
                        }
                        return (
                          <a
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                            {...props}
                          >
                            {children}
                          </a>
                        );
                      },
                    }}
                  >
                    {t(CLIENT_TABS[activeTab].stepsKey)
                      .replace("<GATEWAY_URL>", gatewayUrl)
                      .replace(
                        "<ORG_NAME>",
                        user?.current_org?.display_name ?? "your team",
                      )}
                  </ReactMarkdown>
                </div>
              </div>
            </div>
          </div>

          {/* Advanced: JSON config */}
          <details className="group max-w-xl">
            <summary className="text-sm font-medium text-zinc-700 cursor-pointer select-none">
              {t("connect.advanced")}
            </summary>
            <div className="mt-2 relative">
              <pre className="px-3 py-2 bg-zinc-50 border border-zinc-200 rounded text-sm font-mono text-zinc-900 overflow-x-auto">
                {mcpJsonConfig(gatewayUrl)}
              </pre>
              <div className="absolute top-2 right-2">
                <CopyButton text={mcpJsonConfig(gatewayUrl)} />
              </div>
            </div>
          </details>
        </>
      )}
    </div>
  );
}
