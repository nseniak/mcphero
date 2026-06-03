#!/bin/bash
# Stop all MCP Hero processes and (optionally) Docker containers.
#
# Tears down everything start.sh brings up: backend, frontend.
# Cloud-mode mongo + redis are left running by default (so the next
# ``bash start.sh cloud`` boot stays fast); pass ``--all`` to also
# tear those down.
#
# Usage:
#   bash stop.sh          # stop backend + frontend + sandbox runner
#   bash stop.sh --all    # also stop cloud Docker containers (mongo + redis)

set -e

echo "Stopping MCP Hero..."

# NOTE: scope every kill to LISTEN sockets only — `lsof -ti :PORT` without
# filters also returns processes with client connections to that port
# (e.g. ngrok, in-flight MCP sessions), and a blanket kill -9 would hit
# them too. `-sTCP:LISTEN` keeps us to the actual server process.

# Backend (port 8080)
if lsof -ti :8080 -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -ti :8080 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    echo "  Backend stopped"
else
    echo "  Backend not running"
fi

# Frontend / Vite dev server (port 5173)
if lsof -ti :5173 -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -ti :5173 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    echo "  Frontend stopped"
else
    echo "  Frontend not running"
fi

# Test MCP server (port 9999)
if lsof -ti :9999 -sTCP:LISTEN >/dev/null 2>&1; then
    lsof -ti :9999 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    echo "  Test MCP server stopped"
fi

# Docker containers (mongo + redis, opt-in)
if [ "$1" = "--all" ]; then
    if docker compose ps --quiet 2>/dev/null | grep -q .; then
        docker compose --profile dev --profile cloud --profile standalone down
        echo "  Docker containers stopped"
    else
        echo "  No Docker containers running"
    fi
fi

echo "Done."
