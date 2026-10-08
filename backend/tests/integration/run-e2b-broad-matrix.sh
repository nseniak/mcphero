#!/usr/bin/env bash
# Run JUST the E2B broad-matrix subset on demand (INFRA-3).
#
# The broad-matrix tests exercise the full 24-template E2B grid (node /
# python / docker × 8 CPU/RAM pairs) against a LIVE E2B account. They
# are slow and expensive (one real sandbox per matrix cell).
#
# These now ALSO run in the standard paid integration leg — a plain
# ``bash backend/run-integration-tests.sh`` (and ``make test-all``)
# picks them up whenever ``E2B_API_KEY`` is set and ``NO_INTEGRATION``
# is unset. They carry only the standard ``E2B_API_KEY`` skip marker
# now; the old ``E2B_BROAD_MATRIX`` double-gate is gone. The suite was
# split across ``test_e2b_m_*_e2e.py`` siblings so the integration
# runner's ``--dist loadfile`` mode spreads the cost across the xdist
# workers instead of running the whole sweep serially on one worker.
#
# This wrapper is the run-JUST-the-broad-subset convenience: it selects
# the ``test_e2b_m_*_e2e.py`` files (every broad-matrix function still
# carries an ``e2b_m`` / ``broad_matrix`` token in its node id, so a
# ``-k`` filter also works) and applies the JUnit/JSON reporters. The
# matrix is network-bound (E2B SDK) and total cost (sandbox-seconds) is
# independent of worker count.
#
# Usage:
#   bash backend/tests/integration/run-e2b-broad-matrix.sh                 # whole broad subset, parallel
#   bash backend/tests/integration/run-e2b-broad-matrix.sh -j 1            # serial
#   bash backend/tests/integration/run-e2b-broad-matrix.sh -k tiers -v     # narrow + verbose
#   bash backend/tests/integration/run-e2b-broad-matrix.sh tests/integration/test_e2b_m_tiers_e2e.py
#       ^ that file ONLY: any test path replaces the default broad files.
#
# Parallelism:
#   ``-j N`` (or ``--jobs N``) sets the pytest-xdist worker count
#   (default 4) with ``--dist loadfile`` so each split file lands on a
#   distinct worker.
#
# Outputs, in this run's own folder (tests/run_folder.py), printed at the
# start and the end; /tmp/mcpolis-test-runs/latest-e2b-broad-matrix points
# at the newest:
#   e2b-broad-matrix-junit.xml
#   e2b-broad-matrix-report.json
#
# Secrets (the E2B API key) load from .env.test via
# tests/integration/conftest.py, identical to run-integration-tests.sh.
# Integration tests must never read prod secrets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

JOBS="4"
KEXPR=""
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
        -k)
            # Narrow within the broad subset.
            KEXPR="$2"
            shift 2
            ;;
        *)
            PASSTHRU+=("$1")
            shift
            ;;
    esac
done

# shellcheck disable=SC1091
source "${BACKEND_ROOT}/../run-in-env.sh"
# This run's own results folder, so a concurrent run can't overwrite its
# reports. Resolved before the cd, so a relative MCPOLIS_TEST_OUT_DIR means
# the caller's directory.
RUN_DIR="$(python "${BACKEND_ROOT}/../tests/run_folder.py" e2b-broad-matrix)"
# Exported so pytest prints it again at the very end
# (backend/tests/conftest.py): this script ``exec``s pytest, so that
# signals sent to the runner reach pytest itself.
export MCPOLIS_TEST_OUT_DIR="$RUN_DIR"
cd "${BACKEND_ROOT}"

JUNIT_OUT="$RUN_DIR/e2b-broad-matrix-junit.xml"
JSON_OUT="$RUN_DIR/e2b-broad-matrix-report.json"
rm -f "$JUNIT_OUT" "$JSON_OUT"
echo "Results folder: $RUN_DIR"

PARALLEL_ARGS=()
if [ "$JOBS" != "1" ]; then
    PARALLEL_ARGS=(-n "$JOBS" --dist loadfile)
fi

KEXPR_ARGS=()
if [ -n "$KEXPR" ]; then
    KEXPR_ARGS=(-k "$KEXPR")
fi

# Default to the broad-matrix files ONLY when the caller names no test
# path, the same rule as run-integration-tests.sh (see the note there):
# pytest falls back to ``testpaths`` only when no file, folder or node id
# is on the command line, so a caller's own path runs just that instead
# of the whole broad subset plus that path. The glob is expanded and
# checked here, not left to pytest: when ``testpaths`` matches nothing,
# pytest only warns and collects the whole backend, every paid test.
DEFAULT_PATHS=(tests/integration/test_e2b_m_*_e2e.py)
if [ ! -e "${DEFAULT_PATHS[0]}" ]; then
    echo "ERROR: no broad-matrix files match ${DEFAULT_PATHS[0]}" >&2
    exit 4
fi

exec python -m pytest -o "testpaths=${DEFAULT_PATHS[*]}" \
    "${KEXPR_ARGS[@]+"${KEXPR_ARGS[@]}"}" \
    "${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"}" \
    --junitxml="$JUNIT_OUT" \
    --json-report --json-report-file="$JSON_OUT" --json-report-omit=keywords,streams \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
