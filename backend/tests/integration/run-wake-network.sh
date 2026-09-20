#!/usr/bin/env bash
# Run the post-wake network diagnostic.
#
# Separates "the sandbox's egress is not up yet at resume" from "the
# long-lived process carries a dead pooled socket across the freeze".
# See the module docstring in diagnose_wake_network.py.
#
# Prepares the project Python env (see run-in-env.sh), resolves the E2B
# API key (from env if exported, else from ``backend/.env``), and
# dispatches into ``diagnose_wake_network.py``. Default run is 12
# cycles at 60s paused each: ~15 min wall clock, a few cents of E2B.
# Integration scripts must never read prod secrets — keep the dev key
# in ``backend/.env``.
#
# Knobs:
#   WAKE_CYCLES=12 WAKE_PAUSE_SECONDS=60 WAKE_TARGET_URL=https://...

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
  cat >&2 <<EOF
ERROR: MCPOLIS_E2B_API_KEY (or E2B_API_KEY) is not set, and no
       MCPOLIS_E2B_API_KEY entry was found in
       ${BACKEND_ROOT}/.env

Either export the key:

    export MCPOLIS_E2B_API_KEY=...
    bash backend/tests/integration/run-wake-network.sh

or add it to backend/.env and re-run.
EOF
  exit 2
fi

# Prepare the project Python environment (env-agnostic; see run-in-env.sh).
# shellcheck disable=SC1091
source "${BACKEND_ROOT}/../run-in-env.sh"

cd "${BACKEND_ROOT}"
exec python tests/integration/diagnose_wake_network.py "$@"
