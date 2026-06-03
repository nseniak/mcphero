/**
 * Unit tests for the Sandbox-file slug helper. Mirrors the backend
 * ``slugify_sandbox_file_name`` contract so the frontend preview
 * never lies about what the backend would persist.
 */
import { describe, expect, it } from "vitest";

import {
  isValidSandboxFileName,
  slugifySandboxFileName,
} from "./sandbox-file-slug";

describe("slugifySandboxFileName", () => {
  it("preserves a clean filename verbatim", () => {
    expect(slugifySandboxFileName("credentials.json")).toBe("credentials.json");
    expect(slugifySandboxFileName("kubeconfig.yaml")).toBe("kubeconfig.yaml");
  });

  it("lowercases and dashes a free-form display name", () => {
    expect(slugifySandboxFileName("GCP service account")).toBe(
      "gcp-service-account",
    );
    expect(slugifySandboxFileName("My Cool File")).toBe("my-cool-file");
  });

  it("collapses runs of disallowed chars to a single dash", () => {
    expect(slugifySandboxFileName("foo  /  bar!!.yaml")).toBe(
      "foo-bar-.yaml",
    );
  });

  it("strips leading and trailing dashes", () => {
    expect(slugifySandboxFileName("  -- hello --  ")).toBe("hello");
    expect(slugifySandboxFileName("@@@hello@@@")).toBe("hello");
  });

  it("returns empty string when the input has no usable chars", () => {
    expect(slugifySandboxFileName("@@@")).toBe("");
    expect(slugifySandboxFileName("   ")).toBe("");
    expect(slugifySandboxFileName("")).toBe("");
  });

  it("truncates at 64 chars without leaving a trailing dash", () => {
    const longInput = "a".repeat(80) + "-tail";
    const slug = slugifySandboxFileName(longInput);
    expect(slug.length).toBeLessThanOrEqual(64);
    expect(slug.endsWith("-")).toBe(false);
  });

  it("preserves underscores and dots verbatim (legacy-friendly)", () => {
    // Legacy uppercase-snake names lowercase but otherwise survive.
    expect(slugifySandboxFileName("GCP_CRED")).toBe("gcp_cred");
  });
});

describe("isValidSandboxFileName", () => {
  it("accepts URL-safe slugs", () => {
    expect(isValidSandboxFileName("gcp-cred")).toBe(true);
    expect(isValidSandboxFileName("kubeconfig.dev")).toBe(true);
    expect(isValidSandboxFileName("GCP_CRED")).toBe(true);
    expect(isValidSandboxFileName("a")).toBe(true);
  });

  it("rejects spaces, slashes, and other disallowed chars", () => {
    expect(isValidSandboxFileName("has spaces")).toBe(false);
    expect(isValidSandboxFileName("has/slashes")).toBe(false);
    expect(isValidSandboxFileName("has?query")).toBe(false);
    expect(isValidSandboxFileName("")).toBe(false);
  });

  it("rejects names longer than 64 chars", () => {
    expect(isValidSandboxFileName("a".repeat(64))).toBe(true);
    expect(isValidSandboxFileName("a".repeat(65))).toBe(false);
  });
});
