import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "../api/client";

type AppMode = "standalone" | "cloud";
type SandboxProvider = "e2b" | "local-subprocess";

interface FeaturesResponse {
  allow_stdio_mcp: boolean;
  mode: AppMode;
  sandbox_provider: SandboxProvider;
}

interface UseFeaturesResult {
  allowStdioMcp: boolean;
  mode: AppMode;
  sandboxProvider: SandboxProvider;
  isLoading: boolean;
}

// /api/config/features is effectively constant for a session — its
// values come from server-side env vars that don't change without a
// redeploy. Caching via react-query (with the app-wide
// staleTime=30_000) means subsequent mounts return synchronously
// and routing-decision sites (DefaultRedirect, HomePage) don't
// flash blank while a per-mount round-trip resolves.
//
// Defaults match the historical local-state defaults so the very
// first mount on a cold cache renders sensibly: cloud mode (so
// unauthenticated visitors don't get bounced to /app via the
// standalone redirect on a transient 5xx), and stdio-MCP allowed.
export function useFeatures(): UseFeaturesResult {
  const { data, isLoading } = useQuery({
    queryKey: ["features"],
    queryFn: () => apiFetch<FeaturesResponse>("/api/config/features"),
  });
  return {
    allowStdioMcp: data?.allow_stdio_mcp ?? true,
    mode: data?.mode ?? "cloud",
    sandboxProvider: data?.sandbox_provider ?? "e2b",
    isLoading,
  };
}
