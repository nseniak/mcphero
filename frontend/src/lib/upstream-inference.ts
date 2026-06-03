// Identifiers that are too generic to use as an upstream id — prefer the scope name instead.
const GENERIC_BASENAMES = new Set(["server", "mcp", "mcp-server"]);

export function inferIdFromCommand(_command: string, args: string[]): string {
  for (const arg of args) {
    const hasScope = arg.includes("/");
    const lastSlash = arg.lastIndexOf("/");
    const rawScope = hasScope ? arg.slice(0, lastSlash).replace(/^@/, "") : "";
    // Take only the rightmost path segment as the scope identifier.
    const scope = rawScope.includes("/")
      ? rawScope.slice(rawScope.lastIndexOf("/") + 1)
      : rawScope;
    let basename = hasScope ? arg.slice(lastSlash + 1) : arg;

    // Strip npm version suffix (@latest, @1.2.3) and Docker tag suffix (:latest, :1.0).
    basename = basename.replace(/[@:][^@:/]*$/, "");

    // Strip MCP naming conventions. Combined suffix covers e.g. slack-mcp-server → slack.
    const stripped = basename
      .replace(/^(mcp-server-|server-|mcp-)/, "")
      .replace(/(-mcp-server|-mcp|-server)$/, "");

    if (stripped !== basename) {
      // An MCP prefix/suffix was removed.
      if (GENERIC_BASENAMES.has(stripped) && scope && /^[a-z0-9-]+$/.test(scope)) {
        // Basename reduced to something generic; the scope is the real identity.
        return scope;
      }
      if (stripped && /^[a-z0-9-]+$/.test(stripped)) {
        return stripped;
      }
    } else if (hasScope && stripped && /^[a-z0-9-]+$/.test(stripped)) {
      // Namespaced arg with no MCP prefix (e.g. mcp/sentry:latest → sentry).
      // Still prefer scope over a generic basename.
      if (GENERIC_BASENAMES.has(stripped) && scope && /^[a-z0-9-]+$/.test(scope)) {
        return scope;
      }
      return stripped;
    }
  }
  // No fallback to command basename — launchers like npx/docker/uvx are not useful ids.
  return "";
}

export function inferIdFromUrl(url: string): string {
  try {
    const hostname = new URL(url).hostname;
    // e.g. "mcp.slack.com" → "slack", "api.github.com" → "github"
    const parts = hostname.split(".");
    const filtered = parts.filter(
      (p) => !["www", "mcp", "api", "com", "io", "dev", "org", "net", "co", "app"].includes(p),
    );
    return (filtered[0] || parts[0]).toLowerCase().replace(/[^a-z0-9]/g, "-");
  } catch {
    return "";
  }
}
