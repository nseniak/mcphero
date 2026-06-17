#!/bin/bash
# Thin wrapper around the cross-suite orchestrator (tests/run-all-tests.py).
#
# Runs the unit + e2e + integration suites concurrently under a shared core
# budget so they pass reliably together, not just one at a time. See the
# orchestrator's module docstring for the budget math and env knobs
# (NO_INTEGRATION, UNIT_JOBS, E2E_SHARDS, INTEGRATION_JOBS, E2E_RETRIES,
# E2E_TIMEOUT_MS).
#
# Examples:
#   bash tests/run-all-tests.sh                  # all three, auto-budgeted
#   NO_INTEGRATION=1 bash tests/run-all-tests.sh # skip the paid E2B leg
#   E2E_SHARDS=2 UNIT_JOBS=4 bash tests/run-all-tests.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../run-in-env.sh"
exec python "$SCRIPT_DIR/run-all-tests.py" "$@"
