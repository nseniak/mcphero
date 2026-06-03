# MCP Hero — repo-level helper targets.
#
# Most day-to-day workflows live in ./start.sh, ./stop.sh, and the
# scripts under backend/ and frontend/. This Makefile is for one-shot
# build artifacts that don't fit the dev-loop scripts.
#
# Run targets from the repo root, with the `mcpolis` conda env active:
#   conda activate mcpolis
#   make <target>

.PHONY: help og-image app-icon

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

og-image: ## Re-render frontend/public/og-image.png from frontend/scripts/og-card.svg
	cd frontend && node scripts/generate-og-image.mjs

app-icon: ## Re-render frontend/public/apple-touch-icon.png (transparent) from frontend/public/favicon.svg
	cd frontend && node scripts/generate-app-icon.mjs
