# MCP Hero — repo-level helper targets.
#
# Most day-to-day workflows live in ./start.sh, ./stop.sh, and the
# scripts under backend/ and frontend/. This Makefile is for one-shot
# build artifacts that don't fit the dev-loop scripts.
#
# Run targets from the repo root, with the `mcpolis` conda env active:
#   conda activate mcpolis
#   make <target>

.PHONY: help og-image app-icon github-social test-all

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test-all: ## Run unit + e2e + integration suites concurrently under a shared core budget (NO_INTEGRATION=1 to skip the paid E2B leg)
	bash tests/run-all-tests.sh

og-image: ## Re-render frontend/public/og-image.png from frontend/scripts/og-card.svg
	cd frontend && node scripts/generate-og-image.mjs

app-icon: ## Re-render frontend/public/apple-touch-icon.png (transparent) from frontend/public/favicon.svg
	cd frontend && node scripts/generate-app-icon.mjs

github-social: ## Re-render frontend/public/github-social-preview.png (1280x640) from scripts/og-card.svg for the GitHub repo "Social preview"
	cd frontend && node scripts/generate-github-social.mjs
