/**
 * Mirrors ``backend/tests/unit/test_secret_scanner.py`` to catch
 * drift between the TS and Python implementations of the same
 * regex set. A new pattern added on either side without the matching
 * test on the other side fails CI.
 */
import { describe, expect, it } from "vitest";

import { scanConfigForSecrets } from "./secret-detection";

describe("scanConfigForSecrets", () => {
  it("detects a GitHub personal access token in env", () => {
    const findings = scanConfigForSecrets(
      { GITHUB_TOKEN: "ghp_abcdefghijklmnop1234567890ABCDEF" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("github_token");
    expect(findings[0].field).toBe("env");
    expect(findings[0].key).toBe("GITHUB_TOKEN");
  });

  it("detects an OpenAI / Stripe sk- key", () => {
    const findings = scanConfigForSecrets(
      { OPENAI_API_KEY: "sk-abcdefghijklmnopqrstuvwxyz0123" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("openai_or_stripe_key");
  });

  it("detects an AWS access key", () => {
    const findings = scanConfigForSecrets(
      { X: "AKIAABCDEFGHIJKLMNOP" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("aws_access_key");
  });

  it("detects a Google API key", () => {
    const body = "SyA_abcdefghijklmnopqrstuvwxyz01234"; // exactly 35 chars
    const findings = scanConfigForSecrets(
      { X: `AIza${body}` },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("google_api_key");
  });

  it("detects a Slack token", () => {
    const findings = scanConfigForSecrets(
      { SLACK_TOKEN: "xoxb-123456789012-abcdefABCDEF" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("slack_token");
  });

  it("detects a Stripe live key", () => {
    const findings = scanConfigForSecrets(
      { STRIPE_KEY: "sk_live_abcdefghijklmnop1234567890" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("stripe_live_key");
  });

  it("detects a JWT inside an Authorization header", () => {
    const jwt =
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" +
      ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" +
      ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
    const findings = scanConfigForSecrets(
      undefined,
      { Authorization: `Bearer ${jwt}` },
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("jwt");
    expect(findings[0].field).toBe("headers");
  });

  it("entropy heuristic fires on secret-named keys with high-entropy values", () => {
    const findings = scanConfigForSecrets(
      { MY_SECRET: "abcXYZ012!@#$%^&*()abcXYZ012abcXYZ012" },
      undefined,
    );
    expect(findings).toHaveLength(1);
    expect(findings[0].pattern).toBe("high_entropy");
  });

  it("entropy heuristic does NOT fire on neutral key names", () => {
    const findings = scanConfigForSecrets(
      { NODE_ENV: "abcXYZ012!@#$%^&*()abcXYZ012abcXYZ012" },
      undefined,
    );
    expect(findings).toEqual([]);
  });

  it("does not flag short low-entropy values", () => {
    const findings = scanConfigForSecrets(
      { NODE_ENV: "production", LOG_LEVEL: "debug", PORT: "8080" },
      undefined,
    );
    expect(findings).toEqual([]);
  });

  it("skips already-referenced ${NAME} placeholders", () => {
    const findings = scanConfigForSecrets(
      { GITHUB_TOKEN: "${GITHUB_TOKEN}" },
      undefined,
    );
    expect(findings).toEqual([]);
  });

  it("walks env and headers independently", () => {
    const findings = scanConfigForSecrets(
      { GH: "ghp_abcdefghijklmnop1234567890ABCDEF" },
      { "X-Stripe-Key": "sk_live_abcdefghijklmnop12345" },
    );
    expect(findings).toHaveLength(2);
    expect(new Set(findings.map((f) => f.field))).toEqual(
      new Set(["env", "headers"]),
    );
  });

  it("handles undefined inputs", () => {
    expect(scanConfigForSecrets(undefined, undefined)).toEqual([]);
  });

  it("match preview never contains the full secret", () => {
    const longValue = "ghp_" + "x".repeat(40);
    const findings = scanConfigForSecrets({ X: longValue }, undefined);
    expect(findings).toHaveLength(1);
    const preview = findings[0].matchPreview;
    expect(preview).toBe("ghp_xx…");
    expect(preview).not.toContain(longValue);
  });
});
