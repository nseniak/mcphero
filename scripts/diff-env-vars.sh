#!/usr/bin/env bash
# Compare variable names defined in two env-style files.
#
# Reports:
#   - keys present in <example> but missing from <prod>
#   - keys present in <prod> but missing from <example>
#
# Values are never read, printed, or compared — only key names.
#
# Usage:
#   scripts/diff-env-vars.sh [example-file] [prod-file]
#
# Defaults:
#   example-file = .env.cloud.docker.example
#   prod-file    = .env.cloud.docker.prod

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

example_file="${1:-$repo_root/.env.cloud.docker.example}"
prod_file="${2:-$repo_root/.env.cloud.docker.prod}"

for f in "$example_file" "$prod_file"; do
    if [[ ! -f "$f" ]]; then
        echo "error: file not found: $f" >&2
        exit 2
    fi
done

# Extract KEY names from an env file:
#   - skip blank lines and comments
#   - tolerate optional leading "export "
#   - require a valid identifier followed by '='
extract_keys() {
    local file="$1"
    sed -E \
        -e 's/^[[:space:]]+//' \
        -e 's/^export[[:space:]]+//' \
        -e '/^[[:space:]]*$/d' \
        -e '/^#/d' \
        "$file" \
        | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' \
        | cut -d= -f1 \
        | sort -u
}

example_keys="$(extract_keys "$example_file")"
prod_keys="$(extract_keys "$prod_file")"

only_in_example="$(comm -23 <(printf '%s\n' "$example_keys") <(printf '%s\n' "$prod_keys"))"
only_in_prod="$(comm -13 <(printf '%s\n' "$example_keys") <(printf '%s\n' "$prod_keys"))"

echo "Comparing:"
echo "  example: $example_file"
echo "  prod   : $prod_file"
echo

if [[ -z "$only_in_example" ]]; then
    echo "[ok] every key in example is present in prod"
else
    echo "[missing in prod] keys in example but not in prod:"
    printf '  %s\n' $only_in_example
fi

echo

if [[ -z "$only_in_prod" ]]; then
    echo "[ok] every key in prod is present in example"
else
    echo "[missing in example] keys in prod but not in example:"
    printf '  %s\n' $only_in_prod
fi

if [[ -n "$only_in_example" || -n "$only_in_prod" ]]; then
    exit 1
fi
