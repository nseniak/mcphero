#!/bin/bash
# Audit backend Python dependencies for known CVEs.
# Usage: bash backend/run-security-audit.sh [pip-audit args...]
# Examples:
#   bash backend/run-security-audit.sh                      # full audit, lock-accurate
#   bash backend/run-security-audit.sh --ignore-vuln GHSA-xxxx-xxxx-xxxx
#
# Audits the packages currently installed in the `mcpolis` conda env,
# which mirror `backend/poetry.lock` as long as `poetry install` has
# been run (re-run `poetry install --sync` if the lockfile has moved).
# pip-audit's `--locked` mode is for PEP 751 `pylock.toml`, not
# poetry.lock, so env-mode is the lock-accurate path here.
#
# Outputs (parallel to backend/run-unit-tests.sh's outputs):
#   /tmp/mcpolis-pip-audit.json          (machine-readable)
#   /tmp/mcpolis-pip-audit.txt           (human-readable columns)
#
# Exit code: 0 = clean, 1 = vulnerabilities found (or pip-audit error).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../run-in-env.sh"

JSON_OUT="/tmp/mcpolis-pip-audit.json"
TXT_OUT="/tmp/mcpolis-pip-audit.txt"
rm -f "$JSON_OUT" "$TXT_OUT"

# Run twice: once for human-readable terminal + .txt artifact, once
# for the JSON artifact CI/wrapper scripts can grep. pip-audit caches
# the OSV/PyPI lookups across runs (~/.cache/pip-audit), so the second
# call is near-instant.
pip-audit --local --format columns --output "$TXT_OUT" "$@"
TXT_EXIT=$?

pip-audit --local --format json --output "$JSON_OUT" "$@" >/dev/null 2>&1
JSON_EXIT=$?

cat "$TXT_OUT"

if [ "$TXT_EXIT" -ne 0 ] || [ "$JSON_EXIT" -ne 0 ]; then
    echo
    echo "pip-audit: FAILED — see $TXT_OUT / $JSON_OUT"
    exit 1
fi

echo
echo "pip-audit: clean (logs: $TXT_OUT, $JSON_OUT)"
exit 0
