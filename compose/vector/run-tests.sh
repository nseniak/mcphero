#!/bin/bash
# Run the Vector pipeline's unit tests in a throwaway container.
#
# Usage: bash compose/vector/run-tests.sh
#
# Vector is not installed on dev laptops; we run the same image the
# `cloud` compose profile uses. The `MCPOLIS_ELASTIC_*` env vars are
# referenced by [sinks.elastic] in vector.toml, and Vector validates
# environment substitution even for `vector test`, so we pass throwaway
# values just to satisfy the parser — the sink is never exercised by
# the test runner.
#
# Keep IMAGE in sync with docker-compose.yml's vector service. If they
# drift, dev tests pass on a different version than prod runs.
set -euo pipefail

IMAGE="timberio/vector:0.55.0-debian"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

docker run --rm \
  -e MCPOLIS_ELASTIC_ENDPOINT=https://example.test:443 \
  -e MCPOLIS_ELASTIC_API_KEY=fake \
  -v "${REPO_ROOT}/compose/vector:/etc/vector:ro" \
  "${IMAGE}" \
  test /etc/vector/vector.toml /etc/vector/tests/transforms.toml
