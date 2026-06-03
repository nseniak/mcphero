# mcpolis E2B template grid Makefile.
#
# Targets:
#   make build         publish all 24 templates via the v2 Template SDK
#   make check         CI lint: verify matrix + Dockerfile presence (no API calls)
#   make list-names    print the 24 template names build_grid.py would publish
#   make list          query E2B; list mcpolis-* templates currently published
#   make list-raw      dump the full e2b template list output (debugging)
#   make list-json     JSON dump filtered to mcpolis-*

PYTHON ?= python3
E2B    ?= e2b

.PHONY: build check list-names list list-raw list-json help

help:
	@echo "Available targets:"
	@echo "  make build        — publish 24 templates via the v2 Template SDK"
	@echo "  make check        — CI lint: verify matrix + Dockerfile presence"
	@echo "  make list-names   — print the 24 template names build_grid.py would publish"
	@echo "  make list         — list mcpolis-* templates published to E2B"
	@echo "  make list-raw     — dump full e2b template list (debugging)"
	@echo "  make list-json    — JSON dump filtered to mcpolis-*"

build: ## Publish all 24 templates via the v2 Template SDK driver.
	$(PYTHON) build_grid.py

check: ## CI lint: verify v2 driver matrix + Dockerfile presence (no API calls).
	$(PYTHON) build_grid.py --check

list-names: ## Print template names build_grid.py would publish (no API calls).
	$(PYTHON) build_grid.py --list-only

list: ## List mcpolis-* templates currently published on E2B.
	@$(E2B) template list \
		| grep mcpolis- \
		|| echo "(no mcpolis-* templates found — did you run \`make build\`?)"

list-raw: ## Dump the full e2b template list output (for debugging when the filter misses).
	@$(E2B) template list

list-json: ## JSON dump filtered to mcpolis-*.
	@$(E2B) template list --format json \
		| python3 -c 'import json,sys; ts=json.load(sys.stdin); [print(json.dumps(t, indent=2)) for t in ts if "mcpolis-" in json.dumps(t)]'
