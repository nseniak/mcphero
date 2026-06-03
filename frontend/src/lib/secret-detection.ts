/**
 * Client-side secret-detection mirror of the backend's
 * ``secret_scanner.py``. Run on form submit BEFORE the JSON is sent
 * to the backend, so a detected secret never crosses our trust
 * boundary.
 *
 * Keep regexes in sync with the Python side. A drift here shows up
 * as a value that fires only on one side of the wire.
 */

export type ScanField = "env" | "headers";

export interface ScanFinding {
  field: ScanField;
  key: string;
  pattern: string;
  /** First 6 chars + "…" so the user can spot which value triggered. */
  matchPreview: string;
}

interface ProviderPattern {
  name: string;
  regex: RegExp;
}

const PROVIDER_PATTERNS: ProviderPattern[] = [
  { name: "github_token", regex: /\bgh[psoru]_[A-Za-z0-9]{16,}\b/ },
  { name: "openai_or_stripe_key", regex: /\bsk-[A-Za-z0-9_\-]{20,}\b/ },
  {
    name: "stripe_live_key",
    regex: /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/,
  },
  { name: "aws_access_key", regex: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/ },
  { name: "google_api_key", regex: /\bAIza[0-9A-Za-z_\-]{35}\b/ },
  { name: "slack_token", regex: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/ },
  {
    name: "jwt",
    regex: /\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b/,
  },
];

const SECRET_KEY_RE =
  /(token|secret|key|password|auth|bearer|credential|api[_-]?key)/i;
const PLACEHOLDER_RE = /\$\{[A-Z_][A-Z0-9_]*\}/;

const MIN_ENTROPY_BITS = 4.0;
const MIN_ENTROPY_LENGTH = 16;

function shannonEntropy(value: string): number {
  if (!value) return 0;
  const counts = new Map<string, number>();
  for (const ch of value) {
    counts.set(ch, (counts.get(ch) ?? 0) + 1);
  }
  const length = value.length;
  let total = 0;
  for (const c of counts.values()) {
    const p = c / length;
    total -= p * Math.log2(p);
  }
  return total;
}

function buildPreview(value: string): string {
  return value.length <= 6 ? value : value.slice(0, 6) + "…";
}

function scanValue(
  field: ScanField,
  key: string,
  value: string,
): ScanFinding | null {
  if (PLACEHOLDER_RE.test(value)) return null;
  for (const { name, regex } of PROVIDER_PATTERNS) {
    if (regex.test(value)) {
      return { field, key, pattern: name, matchPreview: buildPreview(value) };
    }
  }
  if (
    SECRET_KEY_RE.test(key) &&
    value.length >= MIN_ENTROPY_LENGTH &&
    shannonEntropy(value) >= MIN_ENTROPY_BITS
  ) {
    return {
      field,
      key,
      pattern: "high_entropy",
      matchPreview: buildPreview(value),
    };
  }
  return null;
}

export function scanConfigForSecrets(
  env: Record<string, string> | undefined,
  headers: Record<string, string> | undefined,
): ScanFinding[] {
  const findings: ScanFinding[] = [];
  if (env) {
    for (const [key, value] of Object.entries(env)) {
      const finding = scanValue("env", key, value);
      if (finding) findings.push(finding);
    }
  }
  if (headers) {
    for (const [key, value] of Object.entries(headers)) {
      const finding = scanValue("headers", key, value);
      if (finding) findings.push(finding);
    }
  }
  return findings;
}
