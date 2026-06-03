#!/bin/bash
# Thin wrapper around the Python E2E orchestrator.
#
# The orchestrator (tests/run-e2e-tests.py) supersedes the original
# bash script — it adds optional sharding and isolated test infra.
# This wrapper is preserved for muscle memory: any existing
# ``bash tests/run-e2e-tests.sh ...`` invocation forwards arguments
# straight through.
#
# Examples:
#   bash tests/run-e2e-tests.sh                        # 1 shard, all tests
#   bash tests/run-e2e-tests.sh --shards 4             # 4 parallel shards
#   bash tests/run-e2e-tests.sh 20-template-vars.spec.ts -g "rename"
#   bash tests/run-e2e-tests.sh --clean                # tear down test infra after
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../run-in-env.sh"
exec python "$SCRIPT_DIR/run-e2e-tests.py" "$@"
