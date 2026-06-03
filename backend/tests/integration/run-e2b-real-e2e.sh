#!/usr/bin/env bash
# Run the E2B real-SDK end-to-end integration script.
#
# Prepares the project Python env (see run-in-env.sh), resolves the E2B
# API key (from env if exported, else from ``backend/.env``), and dispatches into
# ``e2b_real_e2e.py``. Total wall clock is ~3-5 min; the script
# spends ~$0.05 of E2B compute per run. Integration tests must never
# read prod secrets — keep the dev key in ``backend/.env``.

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
       (file ${ENV_FILE:-missing}).

Either export the key:

    export MCPOLIS_E2B_API_KEY=...
    bash backend/tests/integration/run-e2b-real-e2e.sh

or add it to backend/.env and re-run.
EOF
  exit 2
fi

# Prepare the project Python environment (env-agnostic; see run-in-env.sh).
# shellcheck disable=SC1091
source "${BACKEND_ROOT}/../run-in-env.sh"

cd "${BACKEND_ROOT}"
exec python tests/integration/e2b_real_e2e.py "$@"
