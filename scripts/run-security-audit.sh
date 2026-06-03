#!/bin/bash
# Run backend + frontend security audits in parallel.
# Usage: bash scripts/run-security-audit.sh
#
# Wraps backend/run-security-audit.sh (pip-audit) and
# frontend/run-security-audit.sh (npm audit). Run before every deploy
# and after any dependency-touching commit. Dependabot picks up new
# CVEs daily (see .github/dependabot.yml), this script is the
# operator-side gate that catches anything in the gap window.
#
# Aggregate outputs:
#   /tmp/mcpolis-security-audit.log      (combined human-readable log)
#
# Exit code: 0 = both clean, 1 = at least one found findings.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/mcpolis-security-audit.log"
BACKEND_LOG="/tmp/mcpolis-security-audit-backend.log"
FRONTEND_LOG="/tmp/mcpolis-security-audit-frontend.log"
rm -f "$LOG" "$BACKEND_LOG" "$FRONTEND_LOG"

bash "$REPO_ROOT/backend/run-security-audit.sh" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

bash "$REPO_ROOT/frontend/run-security-audit.sh" >"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

wait "$BACKEND_PID"
BACKEND_EXIT=$?

wait "$FRONTEND_PID"
FRONTEND_EXIT=$?

{
    echo "=== backend (pip-audit) ==="
    cat "$BACKEND_LOG"
    echo
    echo "=== frontend (npm audit) ==="
    cat "$FRONTEND_LOG"
    echo
    echo "=== summary ==="
    if [ "$BACKEND_EXIT" -eq 0 ]; then
        echo "backend:  clean"
    else
        echo "backend:  FAILED"
    fi
    if [ "$FRONTEND_EXIT" -eq 0 ]; then
        echo "frontend: clean"
    else
        echo "frontend: FAILED"
    fi
} | tee "$LOG"

if [ "$BACKEND_EXIT" -ne 0 ] || [ "$FRONTEND_EXIT" -ne 0 ]; then
    exit 1
fi
exit 0
