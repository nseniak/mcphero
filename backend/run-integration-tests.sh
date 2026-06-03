#!/bin/bash
# Run pytest against the integration/ directory.
#
# Integration tests hit live external services (E2B today; possibly
# more later) and either spend money OR require credentials. They
# auto-skip when the relevant API key isn't set, so the script is
# safe to run in any environment — but the *intent* is pre-deploy
# verification, not every-PR CI.
#
# Usage:
#   bash backend/run-integration-tests.sh                                # all, parallel
#   bash backend/run-integration-tests.sh -j 1                           # serial
#   bash backend/run-integration-tests.sh tests/integration/test_e2b_sandbox_service_real_sdk.py -v
#
# Parallelism:
#   ``-j N`` (or ``--jobs N``) sets the pytest-xdist worker count.
#   Defaults to 4 — these tests are network-bound (E2B SDK), so more
#   workers buys little and total cost (sandbox-seconds) stays the
#   same regardless of wall-clock compression.
#
# Outputs:
#   /tmp/mcpolis-integration-junit.xml
#   /tmp/mcpolis-integration-report.json
#
# The tests/integration/ directory ALSO hosts run-on-demand standalone
# scripts (e2b_real_e2e.py, list_orphan_sandboxes.py, etc.) — those
# don't match ``test_*.py`` and are skipped by pytest collection.
# Run them via their own per-script wrapper (e.g. run-e2b-real-e2e.sh).
#
# Test config & secrets (the E2B API key) are loaded from .env.test by
# tests/integration/conftest.py, so they resolve the same way whether you
# run this wrapper or a bare ``pytest tests/integration/...``. This script
# only adds parallelism + the JUnit/JSON reporters. See
# tests/integration/.env.test.example for the variables, and conftest.py
# for the loading rules (env wins over file; never reads prod secrets).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

JOBS="4"
PASSTHRU=()
while [ $# -gt 0 ]; do
    case "$1" in
        -j|--jobs)
            JOBS="$2"
            shift 2
            ;;
        -j*)
            JOBS="${1#-j}"
            shift
            ;;
        --jobs=*)
            JOBS="${1#--jobs=}"
            shift
            ;;
        *)
            PASSTHRU+=("$1")
            shift
            ;;
    esac
done

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../run-in-env.sh"
cd "${SCRIPT_DIR}"

JUNIT_OUT="/tmp/mcpolis-integration-junit.xml"
JSON_OUT="/tmp/mcpolis-integration-report.json"
rm -f "$JUNIT_OUT" "$JSON_OUT"

PARALLEL_ARGS=()
if [ "$JOBS" != "1" ]; then
    PARALLEL_ARGS=(-n "$JOBS" --dist loadfile)
fi

# Always scope to tests/integration/ — pytest's default ``testpaths`` in
# pyproject.toml points at tests/unit/ for the offline suite, so without
# an explicit path here we'd accidentally re-run unit tests under
# the integration banner. Extra args (-v, -s, -k, a specific file)
# stack on top.
exec python -m pytest tests/integration/ \
    "${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"}" \
    --junitxml="$JUNIT_OUT" \
    --json-report --json-report-file="$JSON_OUT" --json-report-omit=keywords,streams \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
