import { useEffect, useState } from "react";
import { Cloud, Server } from "lucide-react";

function rootDomain(hostname: string): string {
  const parts = hostname.split(".");
  // Keep last two parts (e.g. "mcp.mixpanel.com" -> "mixpanel.com")
  return parts.length > 2 ? parts.slice(-2).join(".") : hostname;
}

function faviconUrl(url: string): string | null {
  try {
    const { hostname } = new URL(url);
    return `https://www.google.com/s2/favicons?domain=${rootDomain(hostname)}&sz=64`;
  } catch {
    return null;
  }
}

export function UpstreamFavicon({ url, size = 20 }: { url: string | null; size?: number }) {
  const src = url ? faviconUrl(url) : null;
  const [useFavicon, setUseFavicon] = useState(false);

  useEffect(() => {
    if (!src) return;
    const img = new Image();
    img.onload = () => {
      // Google returns a small default globe (16x16) for unknown domains.
      // Real favicons at sz=64 are larger.
      setUseFavicon(img.naturalWidth > 16);
    };
    img.onerror = () => setUseFavicon(false);
    img.src = src;
  }, [src]);

  if (!src || !useFavicon) {
    const Icon = url ? Cloud : Server;
    return <Icon size={size} className="text-zinc-400 shrink-0" />;
  }

  return (
    <img
      src={src}
      alt=""
      width={size}
      height={size}
      className="shrink-0 rounded"
      onError={() => setUseFavicon(false)}
    />
  );
}
