import { Cloud, Server } from "lucide-react";
import { useTranslation } from "../i18n/index";
import { useFeatures } from "../hooks/useFeatures";

interface TransportBadgeProps {
  transport: string;
  withDescription?: boolean;
}

export function TransportBadge({ transport, withDescription = false }: TransportBadgeProps) {
  const { t } = useTranslation();
  const { sandboxProvider } = useFeatures();
  const isStdio = transport === "stdio";
  const stdioKey =
    sandboxProvider === "e2b"
      ? "upstreamDetail.transportStdioDescriptionE2b"
      : "upstreamDetail.transportStdioDescriptionLocal";
  return (
    <span className="inline-flex items-center gap-2">
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium bg-zinc-100 text-zinc-700">
        {isStdio ? <Server size={12} /> : <Cloud size={12} />}
        {isStdio ? "stdio" : "HTTP"}
      </span>
      {withDescription && (
        <span className="text-sm text-zinc-500">
          {isStdio
            ? t(stdioKey)
            : t("upstreamDetail.transportStreamableHttpDescription")}
        </span>
      )}
    </span>
  );
}
