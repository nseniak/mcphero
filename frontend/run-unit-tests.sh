#!/bin/bash
# Run frontend vitest unit tests (jsdom env, no real backend).
# Usage: bash frontend/run-unit-tests.sh [vitest args...]
# Examples:
#   bash frontend/run-unit-tests.sh                                  # full suite + prod build
#   bash frontend/run-unit-tests.sh src/lib/clientErrorReporter.test.ts
#   bash frontend/run-unit-tests.sh -t "blocks Add"                  # name filter
#   bash frontend/run-unit-tests.sh --watch                          # watch mode
#
# Outputs (parallel to backend/run-unit-tests.sh's outputs):
#   /tmp/mcpolis-vitest-junit.xml        (JUnit XML, machine-readable)
#   /tmp/mcpolis-vitest-report.json      (vitest JSON reporter)
#   /tmp/mcpolis-frontend-build.log      (npm run build log; no-arg invocations only)
#
# These files let CI / wrapper scripts grep for pass/fail without
# scraping the human-readable terminal output, same model as the
# pytest wrapper.
#
# No-arg invocations also run `npm run build` in parallel with vitest.
# Vitest transforms via esbuild (laxer than the prod `tsc -b && vite
# build`), so type errors that would fail the host build can pass
# vitest. Catching them here keeps deploys from breaking.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

JUNIT_OUT="/tmp/mcpolis-vitest-junit.xml"
JSON_OUT="/tmp/mcpolis-vitest-report.json"
BUILD_LOG="/tmp/mcpolis-frontend-build.log"
rm -f "$JUNIT_OUT" "$JSON_OUT" "$BUILD_LOG"

RUN_BUILD=0
if [ "$#" -eq 0 ]; then
    RUN_BUILD=1
fi

BUILD_PID=""
if [ "$RUN_BUILD" -eq 1 ]; then
    npm run build >"$BUILD_LOG" 2>&1 &
    BUILD_PID=$!
fi

npx vitest run \
    --reporter=default \
    --reporter=junit --outputFile.junit="$JUNIT_OUT" \
    --reporter=json --outputFile.json="$JSON_OUT" \
    "$@"
VITEST_EXIT=$?

BUILD_EXIT=0
if [ -n "$BUILD_PID" ]; then
    wait "$BUILD_PID"
    BUILD_EXIT=$?
    echo
    echo "--- prod build (npm run build) ---"
    if [ "$BUILD_EXIT" -eq 0 ]; then
        echo "build: passed (log: $BUILD_LOG)"
    else
        echo "build: FAILED — last 40 lines of $BUILD_LOG:"
        tail -40 "$BUILD_LOG"
    fi
fi

if [ "$VITEST_EXIT" -ne 0 ] || [ "$BUILD_EXIT" -ne 0 ]; then
    exit 1
fi
exit 0
