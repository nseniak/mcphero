#!/bin/bash
# Run pyright in the mcpolis conda environment
# Usage: bash backend/run-pyright.sh [pyright args...]
# Examples:
#   bash backend/run-pyright.sh src/
#   bash backend/run-pyright.sh src/mcpolis/domain/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../run-in-env.sh"
cd "$SCRIPT_DIR"
exec python -m pyright "$@"
