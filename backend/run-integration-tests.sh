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
#   bash backend/run-integration-tests.sh -k reattach                    # all, filtered by name
#   bash backend/run-integration-tests.sh tests/integration/test_e2b_sandbox_service_real_sdk.py -v
#       ^ that file ONLY. Any test path (file, folder or ``file::test``
#         node id) replaces the default tests/integration/ folder.
#
# Parallelism:
#   ``-j N`` (or ``--jobs N``) sets the pytest-xdist worker count.
#   Defaults to 4 — these tests are network-bound (E2B SDK), so more
#   workers buys little and total cost (sandbox-seconds) stays the
#   same regardless of wall-clock compression.
#
# Outputs, in this run's own folder (tests/run_folder.py), printed at the
# start and the end; /tmp/mcpolis-test-runs/latest-integration points at
# the newest:
#   integration-junit.xml
#   integration-report.json
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
# This run's own results folder, so a concurrent run can't overwrite its
# reports. Resolved before the cd, so a relative MCPOLIS_TEST_OUT_DIR means
# the caller's directory.
RUN_DIR="$(python "${REPO_ROOT}/tests/run_folder.py" integration)"
# Exported so pytest prints it again at the very end
# (backend/tests/conftest.py): this script ``exec``s pytest, so that
# signals sent to the runner reach pytest itself.
export MCPOLIS_TEST_OUT_DIR="$RUN_DIR"
cd "${SCRIPT_DIR}"

JUNIT_OUT="$RUN_DIR/integration-junit.xml"
JSON_OUT="$RUN_DIR/integration-report.json"
rm -f "$JUNIT_OUT" "$JSON_OUT"
echo "Results folder: $RUN_DIR"

PARALLEL_ARGS=()
if [ "$JOBS" != "1" ]; then
    PARALLEL_ARGS=(-n "$JOBS" --dist loadfile)
fi

# Default to tests/integration/ ONLY when the caller names no test path.
# pyproject.toml's ``testpaths`` points at tests/unit/ for the offline
# suite, so a bare pytest here would re-run the unit tests under the
# integration banner; ``-o testpaths=`` points that default here instead.
# pytest falls back to ``testpaths`` only when no file, folder or node id
# is on the command line, so a caller's ``tests/integration/test_x.py``
# (or ``...::test_y``) runs just that. pytest itself tells a path from an
# option's value, so ``-k expr`` or ``--deselect <id>`` never counts as a
# path. Don't go back to a positional ``tests/integration/``: a caller's
# path then stacks on top of it, and a one-file run became the whole paid
# suite (12 minutes, 2026-10-01).
exec python -m pytest -o testpaths=tests/integration \
    "${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"}" \
    --junitxml="$JUNIT_OUT" \
    --json-report --json-report-file="$JSON_OUT" --json-report-omit=keywords,streams \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
