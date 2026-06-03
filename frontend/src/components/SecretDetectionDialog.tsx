/**
 * Pre-save scan dialog: surfaces likely raw credentials in env /
 * headers, offers a one-click "Move to variables" that extracts the
 * value, creates the env var (defaulting to ``is_secret=true`` since
 * the value was detected as secret-shaped), and rewrites the JSON
 * to ``${NAME}``.
 *
 * Per-finding UI-facing "Treat as password" toggle (the
 * ``is_secret`` flag, default ON) lets the user opt-out for false
 * positives — the value still moves out of the JSON, but lands as
 * a plain visible env var instead of a masked one.
 *
 * Non-blocking: the user can choose to keep the raw value in JSON
 * (with a "Don't show again for this MCP" checkbox to suppress
 * future warnings against that upstream id).
 */
import type { JSX } from "react";
import { useState } from "react";
import { ShieldAlert, X } from "lucide-react";

import type { ScanFinding } from "../lib/secret-detection";

export type DetectionResolution =
  | { kind: "moved"; movedNames: string[] }
  | { kind: "keep" }
  | { kind: "dismissed" };

interface Props {
  open: boolean;
  findings: ScanFinding[];
  /** Used to derive the per-MCP "don't show again" localStorage key.
   *  Pass the upstream id; for the create wizard pass the candidate
   *  id the user typed into the form. */
  upstreamId: string;
  /** Called with the chosen names → values to extract, plus the
   *  resolution outcome. Parent rewrites the JSON env / headers
   *  values to ``${NAME}`` references and saves the env vars either
   *  via API (detail page) or buffered state (create wizard). */
  onResolve: (
    extractions: Array<{
      finding: ScanFinding;
      envVarName: string;
      value: string;
      isSecret: boolean;
    }>,
    resolution: DetectionResolution,
  ) => void;
  /** Look up the original value for each finding (the dialog only
   *  has the truncated preview). Returns the full string. */
  resolveValue: (finding: ScanFinding) => string;
  /** Names of env vars the user already has defined — server-side
   *  for the detail page, or the buffered set for the create wizard.
   *  When a finding's chosen target name matches one of these, the
   *  per-row button switches to "Replace existing <NAME>" and the
   *  name field is locked, so the user makes the replacement
   *  intent explicit instead of accidentally creating an orphan
   *  pair (a leftover env var with no JSON references). */
  existingNames?: Set<string>;
}

const KEY_PREFIX = "mcpolis:secret-scan-dismissed:";

export function isSecretDetectionDismissed(upstreamId: string): boolean {
  if (!upstreamId) return false;
  try {
    return localStorage.getItem(KEY_PREFIX + upstreamId) === "true";
  } catch {
    return false;
  }
}

/** Clear the per-MCP "don't show again" flag so future scans re-show
 *  the detection dialog. Wired to the §H "Show password detection
 *  again" affordance on the upstream detail page. */
export function clearSecretDetectionDismissed(upstreamId: string): void {
  setDismissed(upstreamId, false);
}

function setDismissed(upstreamId: string, dismissed: boolean): void {
  if (!upstreamId) return;
  try {
    if (dismissed) {
      localStorage.setItem(KEY_PREFIX + upstreamId, "true");
    } else {
      localStorage.removeItem(KEY_PREFIX + upstreamId);
    }
  } catch {
    // ignore
  }
}

/**
 * For provider-pattern matches the JSON key is often verbose (e.g.
 * ``GITHUB_PERSONAL_ACCESS_TOKEN``). Suggest a friendlier default per
 * pattern; heuristic-only matches (entropy / key-name) keep the JSON
 * key. Editable in any case.
 */
const PATTERN_NAME_HINTS: Record<string, string> = {
  github_token: "GITHUB_TOKEN",
  openai_or_stripe_key: "OPENAI_API_KEY",
  stripe_live_key: "STRIPE_SECRET_KEY",
  aws_access_key: "AWS_ACCESS_KEY_ID",
  google_api_key: "GOOGLE_API_KEY",
  slack_token: "SLACK_TOKEN",
  jwt: "AUTH_TOKEN",
};

function fromJsonKey(jsonKey: string): string {
  // Best-effort: uppercase + replace anything not [A-Z0-9_] with _.
  let candidate = jsonKey.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
  if (!candidate || !/^[A-Z_]/.test(candidate)) candidate = "_" + candidate;
  return candidate;
}

function defaultTemplateVarName(finding: ScanFinding): string {
  return PATTERN_NAME_HINTS[finding.pattern] ?? fromJsonKey(finding.key);
}

export function SecretDetectionDialog({
  open,
  findings,
  upstreamId,
  onResolve,
  resolveValue,
  existingNames,
}: Props): JSX.Element | null {
  const [names, setNames] = useState<Record<string, string>>(() =>
    Object.fromEntries(findings.map((f) => [f.key, defaultTemplateVarName(f)])),
  );
  const [isSecretByKey, setIsSecretByKey] = useState<Record<string, boolean>>(
    () => Object.fromEntries(findings.map((f) => [f.key, true])),
  );
  const [dontShow, setDontShow] = useState(false);

  if (!open) return null;

  function buildExtractions(toMove: ScanFinding[]) {
    return toMove.map((finding) => ({
      finding,
      envVarName: names[finding.key] ?? defaultTemplateVarName(finding),
      value: resolveValue(finding),
      isSecret: isSecretByKey[finding.key] ?? true,
    }));
  }

  function handleMoveAll() {
    if (dontShow) setDismissed(upstreamId, true);
    onResolve(
      buildExtractions(findings),
      {
        kind: "moved",
        movedNames: findings.map(
          (f) => names[f.key] ?? defaultTemplateVarName(f),
        ),
      },
    );
  }

  function handleKeep() {
    if (dontShow) setDismissed(upstreamId, true);
    onResolve([], { kind: "keep" });
  }

  function handleClose() {
    onResolve([], { kind: "dismissed" });
  }

  function handleMoveOne(finding: ScanFinding) {
    onResolve(
      buildExtractions([finding]),
      {
        kind: "moved",
        movedNames: [names[finding.key] ?? defaultTemplateVarName(finding)],
      },
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={handleClose} />
      <div role="dialog" className="relative bg-white rounded-lg shadow-lg p-6 max-w-lg w-full mx-4 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-semibold text-zinc-900 flex items-center gap-2">
            <ShieldAlert size={16} className="text-amber-600" /> Possible passwords in JSON
          </h3>
          <button onClick={handleClose} className="text-zinc-400 hover:text-zinc-600">
            <X size={16} />
          </button>
        </div>
        <p className="text-sm text-zinc-700">
          One or more values in your JSON look like API keys or
          tokens. Storing them as variables keeps the JSON clean and
          gives you a clear rotation path. Pick a name for each below
          — we'll move the value out of the JSON and replace it with
          <code> {'${NAME}'} </code>.
        </p>
        <ul className="divide-y divide-zinc-200 border border-zinc-200 rounded">
          {findings.map((finding) => {
            const chosenName = names[finding.key] ?? defaultTemplateVarName(finding);
            const isReplacing = existingNames?.has(chosenName) ?? false;
            return (
            <li key={`${finding.field}:${finding.key}`} className="px-3 py-2 space-y-2">
              <div className="flex items-center justify-between text-sm">
                <div>
                  <div className="font-mono text-xs text-zinc-900">
                    {finding.field}.{finding.key}
                  </div>
                  <div className="font-mono text-xs text-zinc-500">
                    {finding.matchPreview} <span className="text-amber-700">({finding.pattern})</span>
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <input
                  value={chosenName}
                  onChange={(e) =>
                    setNames((prev) => ({
                      ...prev,
                      [finding.key]: e.target.value.toUpperCase(),
                    }))
                  }
                  disabled={isReplacing}
                  className="flex-1 px-2 py-1 border border-zinc-300 rounded text-xs font-mono disabled:bg-zinc-100"
                />
                <button
                  type="button"
                  onClick={() => handleMoveOne(finding)}
                  className="px-2 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded"
                >
                  {isReplacing
                    ? `Replace existing ${chosenName}`
                    : "Move to variables"}
                </button>
              </div>
              {!isReplacing && (
                <label className="flex items-center gap-2 text-[11px] text-zinc-600">
                  <input
                    type="checkbox"
                    checked={isSecretByKey[finding.key] ?? true}
                    onChange={(e) =>
                      setIsSecretByKey((prev) => ({
                        ...prev,
                        [finding.key]: e.target.checked,
                      }))
                    }
                  />
                  Treat as password (default — uncheck only for false positives)
                </label>
              )}
            </li>
            );
          })}
        </ul>
        <label className="flex items-center gap-2 text-xs text-zinc-600">
          <input
            type="checkbox"
            checked={dontShow}
            onChange={(e) => setDontShow(e.target.checked)}
          />
          Don't show this again for this MCP
        </label>
        <div className="flex justify-end gap-2">
          <button
            onClick={handleKeep}
            className="px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 rounded"
          >
            Keep in JSON anyway
          </button>
          <button
            onClick={handleMoveAll}
            className="px-3 py-1.5 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded"
          >
            Move all to variables
          </button>
        </div>
      </div>
    </div>
  );
}
