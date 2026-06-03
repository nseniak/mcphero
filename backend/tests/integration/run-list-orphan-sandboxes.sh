#!/usr/bin/env bash
# Convenience wrapper: prepare the project Python env, resolve the
# E2B API key from ``backend/.env`` (or env), and dispatch into
# ``list_orphan_sandboxes.py``. All argv passes through to the
# Python script — see its ``--help`` for flags.
#
# Read-only by default. Use ``--delete-orphans`` to actually kill
# anything; ``--age-min-hours`` to widen/narrow the safety
# threshold; ``--json`` for machine-readable output.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_ROOT="$(cd "${HERE}/../.." && pwd)"

if [[ -z "${MCPOLIS_E2B_API_KEY:-}" && -z "${E2B_API_KEY:-}" ]]; then
  ENV_FILE="${BACKEND_ROOT}/.env"
  if [[ -f "${ENV_FILE}" ]]; then
    KEY="$(grep -E '^MCPOLIS_E2B_API_KEY=' "${ENV_FILE}" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
    if [[ -n "${KEY}" ]]; then
      export MCPOLIS_E2B_API_KEY="${KEY}"
    fi
  fi
fi

if [[ -z "${MCPOLIS_E2B_API_KEY:-}" && -z "${E2B_API_KEY:-}" ]]; then
  echo "ERROR: MCPOLIS_E2B_API_KEY (or E2B_API_KEY) is not set" >&2
  exit 2
fi

# Prepare the project Python environment (env-agnostic; see run-in-env.sh).
# shellcheck disable=SC1091
source "${BACKEND_ROOT}/../run-in-env.sh"

cd "${BACKEND_ROOT}"
exec python tests/integration/list_orphan_sandboxes.py "$@"
