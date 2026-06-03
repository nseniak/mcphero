import { describe, expect, it } from "vitest";
import { inferIdFromCommand, inferIdFromUrl } from "./upstream-inference";

describe("inferIdFromCommand", () => {
  it("extracts scope when npm basename is generic (sentry mcp-server)", () => {
    expect(
      inferIdFromCommand("npx", ["-y", "@sentry/mcp-server@latest", "--host=sentry.mee6.cloud"]),
    ).toBe("sentry");
  });

  it("extracts basename when it is specific (modelcontextprotocol filesystem)", () => {
    expect(
      inferIdFromCommand("npx", ["-y", "@modelcontextprotocol/server-filesystem@latest"]),
    ).toBe("filesystem");
  });

  it("extracts id from unscoped npm package with mcp-server- prefix", () => {
    expect(inferIdFromCommand("npx", ["-y", "mcp-server-git"])).toBe("git");
  });

  it("extracts id from uvx unscoped package with mcp-server- prefix", () => {
    expect(inferIdFromCommand("uvx", ["mcp-server-fetch"])).toBe("fetch");
  });

  it("extracts basename from docker namespaced image (mcp/sentry:latest)", () => {
    expect(inferIdFromCommand("docker", ["run", "-i", "--rm", "mcp/sentry:latest"])).toBe(
      "sentry",
    );
  });

  it("extracts basename from docker registry image with -mcp suffix (ghcr.io/owner/sentry-mcp:latest)", () => {
    expect(
      inferIdFromCommand("docker", ["run", "-i", "--rm", "ghcr.io/owner/sentry-mcp:latest"]),
    ).toBe("sentry");
  });

  it("returns empty string when only flag args are present", () => {
    expect(inferIdFromCommand("npx", ["-y", "--access-token=abc", "--host=example.com"])).toBe("");
  });

  it("returns empty string for empty args", () => {
    expect(inferIdFromCommand("npx", [])).toBe("");
  });

  it("does not fall back to command basename", () => {
    expect(inferIdFromCommand("npx", ["-y"])).toBe("");
    expect(inferIdFromCommand("docker", ["run", "-i", "--rm"])).toBe("");
    expect(inferIdFromCommand("uvx", [])).toBe("");
  });

  it("extracts id from package with -server suffix", () => {
    expect(inferIdFromCommand("npx", ["slack-mcp-server"])).toBe("slack");
  });

  it("extracts scope from scoped package whose basename is just 'server'", () => {
    expect(inferIdFromCommand("npx", ["@acme/server"])).toBe("acme");
  });
});

describe("inferIdFromUrl", () => {
  it("extracts meaningful part from mcp-prefixed subdomain", () => {
    expect(inferIdFromUrl("https://mcp.slack.com/sse")).toBe("slack");
  });

  it("extracts meaningful part from api-prefixed subdomain", () => {
    expect(inferIdFromUrl("https://api.github.com/mcp")).toBe("github");
  });

  it("uses first non-generic part for a plain domain", () => {
    expect(inferIdFromUrl("https://example.com")).toBe("example");
  });

  it("returns empty string for an invalid url", () => {
    expect(inferIdFromUrl("not-a-url")).toBe("");
  });
});
