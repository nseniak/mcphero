#!/bin/bash
# Run pytest in the mcpolis conda environment
# Usage: bash backend/run-unit-tests.sh [-j N|-j auto] [pytest args...]
# Examples:
#   bash backend/run-unit-tests.sh                                # parallel, all tests
#   bash backend/run-unit-tests.sh -j 1 tests/unit/ -v            # serial
#   bash backend/run-unit-tests.sh -j 4                           # 4 workers
#   bash backend/run-unit-tests.sh tests/unit/test_policy_engine.py -x -q
#
# Parallelism:
#   ``-j N`` (or ``--jobs N``) sets the pytest-xdist worker count.
#   Defaults to ``auto`` (xdist picks ~ncpu). ``--dist loadfile``
#   keeps tests from the same file on the same worker so interleaved
#   stdout stays attributable.
#
# Outputs:
#   /tmp/mcpolis-unit-junit.xml          (JUnit XML, machine-readable)
#   /tmp/mcpolis-unit-report.json        (pytest-json-report)
#
# Phase 2c: the test suite includes parameterized repo tests that run
# against both the file backend and a real MongoDB. This script probes
# for a reachable Mongo instance and exports MCPOLIS_TEST_MONGO_URI so
# the conftest can decide whether to run (or skip) the Mongo variants.
# If nothing is listening on localhost:27017, the Mongo parameter is
# skipped and the suite stays green without Docker.
#
# Override the probe by setting MCPOLIS_TEST_MONGO_URI yourself before
# calling the script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../run-in-env.sh"
cd "$SCRIPT_DIR"

JOBS="auto"
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

if [ -z "${MCPOLIS_TEST_MONGO_URI:-}" ]; then
    # Default: talk to Mongo on the user's dev host. Conftest generates
    # a throwaway database name per test run so the real ``mcpolis`` DB
    # is never touched.
    if python -c "import socket, sys; s=socket.socket(); s.settimeout(0.5); s.connect(('localhost', 27017)); s.close()" 2>/dev/null; then
        export MCPOLIS_TEST_MONGO_URI="mongodb://localhost:27017"
        echo "Mongo reachable on localhost:27017 — parameterized Mongo tests enabled"
    else
        echo "No Mongo on localhost:27017 — Mongo-parameterized tests will be skipped"
    fi
fi

# SSRF deny-list (security finding F-01) blocks loopback URLs by
# default. Most unit tests register fake upstreams at 127.0.0.1:<port>
# (test MCP servers, in-process FastAPI test clients), so flip the
# loopback escape hatch on for the unit-test process. The
# ``test_url_safety.py`` tests explicitly ``monkeypatch.delenv`` this
# in helpers so they can still exercise the strict deny-list.
export MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1

JUNIT_OUT="/tmp/mcpolis-unit-junit.xml"
JSON_OUT="/tmp/mcpolis-unit-report.json"
rm -f "$JUNIT_OUT" "$JSON_OUT"

PARALLEL_ARGS=()
if [ "$JOBS" != "1" ]; then
    PARALLEL_ARGS=(-n "$JOBS" --dist loadfile)
fi

# Rerun safety net for transient connection blips under load — the unit
# leg's analogue of the e2e suite's Playwright ``retries``. OFF by
# default (UNIT_RERUNS=0) so a standalone run stays deterministic and a
# real flake is visible to whoever's debugging; ``make test-all`` sets
# UNIT_RERUNS>0 (see run-all-tests.py) for the oversubscribed
# cross-suite run, where a CPU-/Docker-VM-starved box intermittently
# refuses or times out the connect to Redis (coredis), Mongo (pymongo),
# or an in-process loopback server (httpx) — the exact analogue of the
# e2e socket blips.
#
# Reruns are UNSCOPED (no ``--only-rerun``), matching the e2e leg's
# retries exactly. An earlier scoped attempt was abandoned after
# verifying ``--only-rerun`` matches only the OUTER exception's text:
# these connection failures surface WRAPPED (anyio BrokenResourceError,
# ExceptionGroup, pymongo TimeoutError), so scoping silently missed them.
# The safety against masking a real bug is structural, not pattern-based:
# (a) UNIT_RERUNS=0 standalone, so dev/debug runs never rerun; (b) the
# deterministic logic flakes are fixed at the source (clock injection,
# subscribe-ready, readiness gates), so reruns are not papering over a
# logic race; (c) a deterministic failure still fails every attempt.
RERUN_ARGS=()
UNIT_RERUNS="${UNIT_RERUNS:-0}"
if [ "$UNIT_RERUNS" != "0" ]; then
    RERUN_ARGS=(--reruns "$UNIT_RERUNS" --reruns-delay 1)
fi

exec python -m pytest \
    "${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"}" \
    "${RERUN_ARGS[@]+"${RERUN_ARGS[@]}"}" \
    --junitxml="$JUNIT_OUT" \
    --json-report --json-report-file="$JSON_OUT" --json-report-omit=keywords,streams \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
