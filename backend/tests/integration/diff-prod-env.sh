#!/usr/bin/env bash
# Compare the variable set in the gitignored prod env file against
# the checked-in example. Useful before ``make secrets-push`` to
# catch drift — vars added to the example but not yet in prod, or
# vars left in prod that the example has retired.
#
# Prints var NAMES only, never values, so it's safe to run
# anywhere (no secrets cross the boundary).

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EXAMPLE="${1:-$repo_root/.env.cloud.docker.example}"
PROD="${2:-$repo_root/.env.cloud.docker.prod}"

if [[ ! -f "$EXAMPLE" ]]; then
  echo "ERROR: example not found at $EXAMPLE" >&2
  exit 1
fi
if [[ ! -f "$PROD" ]]; then
  echo "ERROR: prod env not found at $PROD" >&2
  exit 1
fi

# Extract uncommented ``KEY=...`` lines, take just the KEY,
# uniquify, sort. Ignores blank lines and comments.
extract_keys() {
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$1" | cut -d= -f1 | sort -u
}

EXAMPLE_KEYS="$(extract_keys "$EXAMPLE")"
PROD_KEYS="$(extract_keys "$PROD")"

ONLY_IN_EXAMPLE="$(comm -23 <(echo "$EXAMPLE_KEYS") <(echo "$PROD_KEYS"))"
ONLY_IN_PROD="$(comm -13 <(echo "$EXAMPLE_KEYS") <(echo "$PROD_KEYS"))"
IN_BOTH="$(comm -12 <(echo "$EXAMPLE_KEYS") <(echo "$PROD_KEYS"))"

# For vars present in both, flag whether the *values* differ —
# without printing them. Catches "you forgot to bump the value in
# prod after the example changed" without leaking secrets.
VALUE_DIFFERS=""
while IFS= read -r key; do
  [[ -z "$key" ]] && continue
  example_val="$(grep -E "^${key}=" "$EXAMPLE" | head -1 | cut -d= -f2-)"
  prod_val="$(grep -E "^${key}=" "$PROD" | head -1 | cut -d= -f2-)"
  if [[ "$example_val" != "$prod_val" ]]; then
    VALUE_DIFFERS+="${key}"$'\n'
  fi
done <<< "$IN_BOTH"

print_section() {
  local label="$1"; local body="$2"
  echo "==> $label"
  if [[ -z "$body" ]]; then
    echo "  (none)"
  else
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      echo "  $line"
    done <<< "$body"
  fi
  echo
}

echo "diff between:"
echo "  example: $EXAMPLE"
echo "  prod:    $PROD"
echo

print_section "missing from prod (in example only — operator should add)" "$ONLY_IN_EXAMPLE"
print_section "extra in prod (not in example — example may be stale, or var retired)" "$ONLY_IN_PROD"
print_section "value differs (present in both, different values)" "$VALUE_DIFFERS"
