import { useEffect, useState } from "react";
import { fetchStartupStatus } from "../api/admin";
import type { StartupStatus } from "../api/admin";
import { useTranslation } from "../i18n/index";
import { Loader2 } from "lucide-react";

export function StartupBanner() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<StartupStatus | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const s = await fetchStartupStatus();
        if (active) setStatus(s);
        if (!s.ready && active) {
          setTimeout(poll, 1000);
        }
      } catch {
        // Server not ready yet, retry
        if (active) setTimeout(poll, 2000);
      }
    };
    poll();
    return () => { active = false; };
  }, []);

  if (dismissed || !status || status.ready) return null;

  const progress = status.total > 0
    ? Math.round(((status.connected + status.failed) / status.total) * 100)
    : 0;

  return (
    <div className="bg-blue-50 border-b border-blue-200 px-6 py-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Loader2 size={16} className="text-blue-600 animate-spin" />
          <span className="text-sm text-blue-800">
            {t("startup.connecting", {
              connected: status.connected,
              total: status.total,
            })}
          </span>
          {status.failed > 0 && (
            <span className="text-xs text-amber-600">
              ({t("startup.failed", { count: status.failed })})
            </span>
          )}
        </div>
        <button
          onClick={() => setDismissed(true)}
          className="text-xs text-blue-500 hover:text-blue-700"
        >
          {t("startup.dismiss")}
        </button>
      </div>
      <div className="mt-2 h-1.5 bg-blue-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-blue-500 rounded-full transition-all duration-500"
          style={{ width: `${progress}%` }}
        />
      </div>
    </div>
  );
}
