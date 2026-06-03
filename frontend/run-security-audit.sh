#!/bin/bash
# Audit frontend npm dependencies for known CVEs.
# Usage: bash frontend/run-security-audit.sh [npm audit args...]
# Examples:
#   bash frontend/run-security-audit.sh                     # high+critical, fail-on-find
#   bash frontend/run-security-audit.sh --audit-level=moderate
#
# Reads `frontend/package-lock.json` via `npm audit`, so the audited
# set matches what gets installed in production.
#
# Default severity threshold is `high` — moderate/low findings are
# reported but don't fail the gate (npm's default tends to flood with
# transitive low-severity issues that aren't actually exploitable in
# our usage). Override with `--audit-level=...`.
#
# Outputs (parallel to frontend/run-unit-tests.sh's outputs):
#   /tmp/mcpolis-npm-audit.json          (machine-readable)
#   /tmp/mcpolis-npm-audit.txt           (human-readable)
#
# Exit code: 0 = no findings at or above threshold, 1 = findings.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

JSON_OUT="/tmp/mcpolis-npm-audit.json"
TXT_OUT="/tmp/mcpolis-npm-audit.txt"
rm -f "$JSON_OUT" "$TXT_OUT"

# Default to --audit-level=high unless the caller passes their own.
LEVEL_ARG="--audit-level=high"
for arg in "$@"; do
    case "$arg" in
        --audit-level=*) LEVEL_ARG="" ;;
    esac
done

# Two passes for the same reason as the backend audit: terminal-friendly
# columns + machine-readable JSON. npm audit's response is cached on
# the registry side (no rate concern at our scale).
npm audit $LEVEL_ARG "$@" 2>&1 | tee "$TXT_OUT"
TXT_EXIT=${PIPESTATUS[0]}

npm audit --json $LEVEL_ARG "$@" >"$JSON_OUT" 2>/dev/null
JSON_EXIT=$?

if [ "$TXT_EXIT" -ne 0 ] || [ "$JSON_EXIT" -ne 0 ]; then
    echo
    echo "npm audit: FAILED — see $TXT_OUT / $JSON_OUT"
    exit 1
fi

echo
echo "npm audit: clean (logs: $TXT_OUT, $JSON_OUT)"
exit 0
