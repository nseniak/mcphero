#!/bin/bash
# shellcheck disable=SC2218
# (SC2218 false-positive: `ensure_docker_running` is defined at the
# top of the file but shellcheck doesn't trace bash's late binding
# across the elif chain. Bash resolves names when called.)
#
# Start (or restart) backend + frontend in standalone or cloud mode.
#
# Usage:
#   bash start.sh                       cloud (default, against docker compose dev)
#   bash start.sh cloud                 same as above, explicit
#   bash start.sh standalone            standalone mode (file-backed, no docker)
#   bash start.sh --fake-auth           cloud mode with the dev-stub auth flag
#   bash start.sh standalone --no-demo  flags can sit on either side of the mode
#
# Cloud mode:
#   - Ensures Docker Desktop is running (opens it if not)
#   - Ensures the ``dev`` compose profile is up
#   - Loads dev secrets from ``backend/.env.cloud`` (auto-created from
#     ``.env.cloud.example`` on first run)
#   - Persistent ``mcpolis_dev`` database — tokens survive restarts
#   - Containers are left running on exit so the next boot is fast
#
# Frontend is started in both modes; the frontend is mode-agnostic and
# talks to whichever backend is on :8080.
#
# Optional flags (any position):
#   --fake-auth    Skip Google OAuth: enable the in-app dev-stub email
#                  picker for the dashboard AND the gateway's
#                  test-bearer-token endpoint. Use only in dev — both
#                  paths are explicitly opt-in security holes.
#   --no-demo      Skip mounting the bundled demo MCP server.
#   --with-demo    No-op (the demo is on by default; flag kept for
#                  back-compat with old shell history).
#
# Backend, frontend, and the sandbox runner are all auto-started: each
# one is started if not already running, skipped otherwise. The
# sandbox runner needs Docker; if Docker can't be brought up, the
# runner is skipped and stdio MCPs fall back to the unsafe
# local-subprocess path with a clear warning at boot. Use stop.sh to
# tear everything down.
set -e

# Resolve repo root (the directory containing this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Parse arguments — flags can sit anywhere; the optional positional
# arg is the mode (``cloud`` is the default). This is friendlier than
# the old positional-mode-then-flags shape: ``bash start.sh
# --fake-auth`` now does the obvious thing.

MODE=""
# Demo MCP is on by default in dev — both mount AND seed so the local
# dashboard ships with a working demo upstream. Prod gates these
# independently (mount-only, never seed). ``--no-demo`` clears both.
export MCPOLIS_DEMO_MOUNT=1
export MCPOLIS_DEMO_SEED=1
for arg in "$@"; do
    case "$arg" in
        cloud|standalone)
            if [ -n "$MODE" ] && [ "$MODE" != "$arg" ]; then
                echo "Conflicting modes: $MODE and $arg"
                exit 1
            fi
            MODE="$arg"
            ;;
        --fake-auth)
            export MCPOLIS_OAUTH_PROVIDER=dev_stub
            export MCPOLIS_TEST_MODE=1
            echo "Fake-auth: MCPOLIS_OAUTH_PROVIDER=dev_stub (dashboard email picker) + MCPOLIS_TEST_MODE=1 (gateway test-mcp-token)"
            ;;
        --no-demo)
            unset MCPOLIS_DEMO_MOUNT
            unset MCPOLIS_DEMO_SEED
            echo "Demo upstream disabled"
            ;;
        --with-demo)
            # No-op since the demo is on by default. Kept so old shell
            # history doesn't error out.
            ;;
        --with-stdio-test)
            # Opt in to the controllable stdio MCP test fixture. Off
            # by default (it's a fixture, not a demo); flip on for
            # manual exploration in dev. Mirrors --no-demo's shape.
            # The route at /dev/stdio-test-mcp.py serves the script;
            # the test harness ``uv run``s it inside an E2B sandbox.
            export MCPOLIS_STDIO_TEST_MCP=1
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: bash start.sh [cloud|standalone] [--fake-auth] [--no-demo] [--with-stdio-test]"
            exit 1
            ;;
    esac
done
MODE="${MODE:-cloud}"
if [ -n "${MCPOLIS_DEMO_MOUNT:-}" ]; then
    echo "Demo upstream enabled (mounted at /dev/mcp-demo, seed=${MCPOLIS_DEMO_SEED:-0})"
fi

# Stdio MCPs default to OFF in cloud mode (multi-tenant safety). Force
# them on in dev so the sandbox runner can be smoke-tested end-to-end.
export MCPOLIS_ALLOW_STDIO_MCP=true

# SSRF deny-list (security finding F-01) blocks loopback URLs by
# default. Dev needs the bundled demo MCP at
# ``http://localhost:8080/dev/mcp-demo`` to register, so flip the
# loopback escape hatch on for both the validator and the runtime
# transport. Prod NEVER sets this — the strict deny-list is what
# closes the SSRF.
export MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1

# Returns 0 if Docker is running and reachable, attempting to start
# Docker Desktop (and waiting up to 60s) if it isn't. Returns 1 when
# Docker can't be brought up — callers decide whether to abort or
# continue without it.
ensure_docker_running() {
    if docker info >/dev/null 2>&1; then
        return 0
    fi
    echo "Docker is not running. Starting Docker Desktop..."
    open -a Docker
    echo -n "Waiting for Docker"
    for _ in $(seq 1 60); do
        if docker info >/dev/null 2>&1; then
            echo " ready."
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo
    return 1
}

# shellcheck disable=SC1091
source "$SCRIPT_DIR/run-in-env.sh"

# --- Cloud mode prep: Docker Desktop + compose + env file --------------

if [ "$MODE" = "cloud" ]; then
    # 0. Pre-flight: fail fast if 6379 or 27017 are bound by something
    #    that isn't one of our own containers. Docker's own error on
    #    `compose up` is cryptic; surface the real culprit up front.
    check_port() {
        local port=$1
        local expected_container=$2
        local holder
        holder=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (pid "$2")"}')
        if [ -z "$holder" ]; then
            return 0
        fi
        # If the port is held by our own container, that's fine.
        local owning_container
        owning_container=$(docker ps --filter "name=$expected_container" --filter "status=running" --format "{{.Names}}")
        if [ -n "$owning_container" ]; then
            return 0
        fi
        # Try to name a conflicting Docker container (common case on dev laptops).
        local conflicting_container
        conflicting_container=$(docker ps --filter "publish=$port" --format "{{.Names}} ({{.Image}})" | grep -v "^$expected_container" | head -1)
        echo "ERROR: Port $port is already in use by $holder." >&2
        if [ -n "$conflicting_container" ]; then
            echo "       Conflicting Docker container: $conflicting_container" >&2
            echo "       Stop it with:  docker stop ${conflicting_container%% *}" >&2
        fi
        echo "       $expected_container needs this port. Free it and retry." >&2
        return 1
    }
    preflight_failed=0
    check_port 6379 mcpolis-redis || preflight_failed=1
    check_port 27017 mcpolis-mongo || preflight_failed=1
    if [ "$preflight_failed" = "1" ]; then
        exit 1
    fi

    # 1. Make sure Docker is running.
    if ! ensure_docker_running; then
        echo "ERROR: Docker did not start within 60 seconds. Aborting." >&2
        exit 1
    fi

    # 2. Bring up mongo + redis from the dev profile if they're not
    #    already running. Compose is idempotent but we skip the call
    #    when both containers are healthy to avoid unnecessary noise.
    mongo_running=$(docker ps --filter "name=mcpolis-mongo" --filter "status=running" --format "{{.Names}}")
    redis_running=$(docker ps --filter "name=mcpolis-redis" --filter "status=running" --format "{{.Names}}")
    if [ -z "$mongo_running" ] || [ -z "$redis_running" ]; then
        echo "Starting dev containers (mongo + redis)..."
        docker compose --profile dev up -d
    else
        echo "Dev containers already running (mongo + redis)"
    fi

    # 3. Wait for Mongo to accept connections.
    echo -n "Waiting for Mongo"
    for _ in $(seq 1 20); do
        if docker exec mcpolis-mongo mongosh --quiet --eval "db.runCommand({ping:1})" >/dev/null 2>&1; then
            echo " ready."
            break
        fi
        echo -n "."
        sleep 1
    done

    # 4. Auto-copy the example env file on first run, then load it.
    if [ ! -f backend/.env.cloud ]; then
        if [ -f backend/.env.cloud.example ]; then
            cp backend/.env.cloud.example backend/.env.cloud
            echo "Created backend/.env.cloud from .env.cloud.example"
            echo "  (edit it to rotate dev secrets; it is gitignored)"
        else
            echo "ERROR: backend/.env.cloud.example is missing"
            exit 1
        fi
    fi

    # Source ``.env.cloud`` but don't overwrite variables that are
    # already set in the environment (e.g. MCPOLIS_MONGO_DB_NAME
    # set by run-e2e-tests.sh for database isolation).
    set -a
    # shellcheck disable=SC1091
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        key=$(echo "$key" | xargs)  # trim whitespace
        if [ -z "${!key:-}" ]; then
            export "$key=$value"
        fi
    done < backend/.env.cloud
    set +a
fi

# --- Sandbox provider --------------------------------------------------
#
# stdio MCPs run via the SandboxService boundary. Cloud / hosted dev
# defaults to E2B (set MCPOLIS_E2B_API_KEY in backend/.env.cloud);
# without an API key the backend falls back to the unsafe
# local-subprocess path with a clear warning at startup.

# --- Frontend dist cleanup (both modes) --------------------------------

# Remove any stale production build — dev should always go through Vite
if [ -d frontend/dist ]; then
    echo "Removing stale frontend/dist (dev should use Vite, not a built bundle)"
    rm -rf frontend/dist
fi

# --- Kill existing backend --------------------------------------------

# Scope to LISTEN sockets only — `lsof -ti :8080` without filters also
# returns any process with a client connection to 8080 (e.g. ngrok,
# in-flight MCP clients), and a blanket kill -9 would take them out too.
lsof -ti :8080 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# --- Start backend ----------------------------------------------------

echo "Starting backend in $MODE mode..."
(cd backend && nohup python -m mcpolis > /tmp/mcpolis-backend.log 2>&1 &)
echo "Backend started (log: /tmp/mcpolis-backend.log)"

# --- Start frontend if not running ------------------------------------

if ! curl -s http://localhost:5173 -o /dev/null -w '' 2>/dev/null; then
    (cd frontend && nohup npm run dev > /tmp/mcpolis-frontend.log 2>&1 &)
    echo "Frontend started (log: /tmp/mcpolis-frontend.log)"
else
    echo "Frontend already running"
fi

# --- Wait for backend + frontend health -------------------------------

wait_for() {
    local label=$1
    local url=$2
    echo -n "Waiting for $label..."
    for _ in $(seq 1 15); do
        if curl -s "$url" > /dev/null 2>&1; then
            echo " ready!"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo " FAILED"
    return 1
}

wait_for backend http://localhost:8080/health || {
    echo "check /tmp/mcpolis-backend.log"
    exit 1
}
wait_for frontend http://localhost:5173/ || {
    echo "check /tmp/mcpolis-frontend.log"
    exit 1
}
echo "MCP Hero is up in $MODE mode."
