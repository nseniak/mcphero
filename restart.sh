#!/bin/bash
# Restart backend + frontend so the operator can manually exercise
# the latest changes in local dev. Thin wrapper around stop.sh +
# start.sh; all flags forward to start.sh (e.g. ``bash restart.sh
# standalone --fake-auth``).
#
# Cloud-mode containers (mongo + redis) are deliberately left up
# across the cycle — restarting them would slow each iteration to
# tens of seconds for no gain. Use ``bash stop.sh --all`` if you
# really want to nuke them.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$SCRIPT_DIR/stop.sh"
bash "$SCRIPT_DIR/start.sh" "$@"
