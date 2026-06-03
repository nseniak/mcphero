#!/bin/sh
set -e

# Seed missing config files from init defaults
[ -f /app/config/config.json ] || cp /app/config-init/config.json /app/config/config.json
[ -f /app/config/mcp.json ] || cp /app/config-init/mcp.json /app/config/mcp.json
[ -f /app/config/oauth_apps.json ] || cp /app/config-init/oauth_apps.json /app/config/oauth_apps.json

exec python -m mcpolis "$@"
