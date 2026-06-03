#!/bin/bash
# Convenience script for testing whether Anthropic's connector
# auto-reinits on 404 after a gateway restart.
#
# Usage:
#   bash scripts/test-session-survival.sh redeploy
#   bash scripts/test-session-survival.sh watch
#
# See internal/documents/widget-forwarding.md for the test rationale.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/.."
cd "$REPO_ROOT"

case "${1:-}" in
    redeploy)
        echo "[$(date +%H:%M:%S)] Restarting gateway (cloud + demo)..."
        bash start.sh cloud --with-demo > /dev/null
        echo "[$(date +%H:%M:%S)] Gateway restarted."
        ;;
    watch)
        echo "Tailing /tmp/mcpolis-backend.log for the events relevant to the test."
        echo "Look for:"
        echo "  - 'session.stale.rejected' immediately after a redeploy = Anthropic sent a stale ID"
        echo "  - 'gateway.tool.call.received' / 'gateway.tool.call.multi_org.received' = call landed on a fresh session"
        echo "  - 404 in the uvicorn access line = Anthropic did NOT auto-reinit; you'd see an error in chat"
        echo "  - 200 right after = Anthropic re-initialized; chat tool call succeeded"
        echo
        echo "Press Ctrl-C to stop."
        echo "---"
        tail -F /tmp/mcpolis-backend.log 2>/dev/null | \
            grep --line-buffered -iE "session.stale|tool.call|/mcp HTTP/1.1\" (200|404)|gateway.tool"
        ;;
    *)
        echo "Usage: bash scripts/test-session-survival.sh {redeploy|watch}"
        echo
        echo "Recommended workflow (two terminals):"
        echo "  Terminal 1:  bash scripts/test-session-survival.sh watch"
        echo "  Terminal 2:  bash scripts/test-session-survival.sh redeploy   # between Claude.ai tool calls"
        exit 1
        ;;
esac
