#!/bin/bash
# Standalone-mode smoke test. Starts the backend in standalone mode on
# a non-default port, hits a handful of endpoints to catch routing-
# layer regressions, then tears down.
#
# Intended as a cheap safety net for refactors that touch the
# middleware or the mode branches — runs in ~10 seconds without
# Playwright, without touching the user's running dev backend.
#
# Usage:
#   bash backend/run-standalone-smoke.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/.."
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source run-in-env.sh

PORT=18080
HEALTH_URL="http://localhost:$PORT/health"

# Fresh tmp dirs so we don't stomp the user's standalone state.
SMOKE_DATA=$(mktemp -d -t mcpolis-smoke-XXXXXX)
SMOKE_CONFIG=$(mktemp -d -t mcpolis-smoke-cfg-XXXXXX)
LOG=/tmp/mcpolis-smoke-backend.log

# Minimal configs — empty but syntactically valid.
echo '{}' > "$SMOKE_CONFIG/config.json"
echo '{"mcpServers": {}}' > "$SMOKE_CONFIG/mcp.json"
echo '{}' > "$SMOKE_CONFIG/oauth_apps.json"

# Kill anything listening on our port (shouldn't be anything — chosen
# specifically to avoid the user's dev server on 8080).
lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true

cleanup() {
    if [ -n "${BACKEND_PID:-}" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    # Also scrub anything still on the port, in case.
    lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
    rm -rf "$SMOKE_DATA" "$SMOKE_CONFIG"
}
trap cleanup EXIT

# Start the backend with a completely isolated env so it doesn't pick
# up the user's .env (which may have OAuth enabled against real Google
# creds). We want the minimum viable standalone config.
echo "Starting standalone backend on :$PORT..."
# Isolate from the user's backend/.env by running in a tmp cwd so
# pydantic-settings can't pick it up. We copy src/ or rely on Python's
# import paths — actually, easier: override env_file with an empty
# file by pointing HOME and cwd away. Simpler still: pass the full
# config via env and move to tmp cwd.
env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    MCPOLIS_MODE=standalone \
    MCPOLIS_OAUTH_PROVIDER=google \
    MCPOLIS_GOOGLE_CLIENT_ID=smoke-dummy-id \
    MCPOLIS_GOOGLE_CLIENT_SECRET=smoke-dummy-secret \
    MCPOLIS_HOST=127.0.0.1 \
    MCPOLIS_PORT=$PORT \
    MCPOLIS_DATA_DIR="$SMOKE_DATA" \
    MCPOLIS_CONFIG_PATH="$SMOKE_CONFIG/config.json" \
    MCPOLIS_MCP_JSON_PATH="$SMOKE_CONFIG/mcp.json" \
    MCPOLIS_OAUTH_APPS_PATH="$SMOKE_CONFIG/oauth_apps.json" \
    MCPOLIS_AUDIT_LOG_PATH="$SMOKE_DATA/audit.jsonl" \
    MCPOLIS_SERVER_URL="http://localhost:$PORT" \
    MCPOLIS_SESSION_SECRET="smoke-dummy-session-secret-do-not-use" \
    MCPOLIS_SENTRY_DSN="" \
    MCPOLIS_MIXPANEL_TOKEN="" \
    bash -c "cd '$SMOKE_CONFIG' && python -m mcpolis" \
    > "$LOG" 2>&1 &
BACKEND_PID=$!

# Wait for health.
for _ in $(seq 1 30); do
    if curl -s "$HEALTH_URL" > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

if ! curl -s "$HEALTH_URL" > /dev/null 2>&1; then
    echo "ERROR: backend didn't come up. Last log lines:"
    tail -40 "$LOG"
    exit 1
fi

FAIL=0

check() {
    local desc="$1"
    local url="$2"
    local expected="$3"
    local actual
    actual=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    if [ "$actual" = "$expected" ]; then
        echo "  ok   $desc ($actual)"
    else
        echo "  FAIL $desc: expected $expected, got $actual"
        FAIL=1
    fi
}

check_in() {
    # Assert status is one of a whitelist (for endpoints where several
    # statuses are all "routing is fine").
    local desc="$1"
    local url="$2"
    shift 2
    local actual
    actual=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    for expected in "$@"; do
        if [ "$actual" = "$expected" ]; then
            echo "  ok   $desc ($actual)"
            return
        fi
    done
    echo "  FAIL $desc: expected one of [$*], got $actual"
    FAIL=1
}

echo "Smoke-testing standalone endpoints:"
check    "/health returns 200" "http://localhost:$PORT/health" 200
check    "/api/config/features returns 200" "http://localhost:$PORT/api/config/features" 200
# MCP mount root — exact status depends on the Streamable-HTTP
# implementation; we only care that the route isn't a 404.
check_in "/mcp/ is routed (not 404)" "http://localhost:$PORT/mcp/" 200 307 400 401 405 406 500
# Well-known metadata must be reachable.
check_in "/mcp/.well-known/oauth-protected-resource reachable" \
    "http://localhost:$PORT/mcp/.well-known/oauth-protected-resource" 200 307
check_in "/mcp/.well-known/oauth-authorization-server reachable" \
    "http://localhost:$PORT/mcp/.well-known/oauth-authorization-server" 200 307

# Mode reported as standalone in the features endpoint.
MODE=$(curl -s "http://localhost:$PORT/api/config/features" | \
    python3 -c 'import sys,json; print(json.load(sys.stdin).get("mode", ""))')
if [ "$MODE" = "standalone" ]; then
    echo "  ok   features reports mode=standalone"
else
    echo "  FAIL features reports mode='$MODE'"
    FAIL=1
fi

if [ "$FAIL" = "1" ]; then
    echo "Standalone smoke: FAILED"
    exit 1
fi
echo "Standalone smoke: PASSED"
