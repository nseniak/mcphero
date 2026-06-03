# --- Frontend build stage ---
FROM node:20-slim AS frontend-build
WORKDIR /build

# Install Debian's chromium so the prerender plugin's puppeteer
# integration has a working browser binary on aarch64. Puppeteer's
# bundled Chrome download fails on arm64 ("Syntax error: redirection
# unexpected" — the linux_arm cache directory contains an x86 wrapper).
# Skip puppeteer's auto-download and point it at the system binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

ARG VITE_SENTRY_DSN=""
ARG VITE_SENTRY_TRACES_SAMPLE_RATE="0.1"
ARG VITE_MIXPANEL_TOKEN=""
ARG VITE_MIXPANEL_API_HOST=""
ARG VITE_RELEASE=""
ARG VITE_SITE_URL=""
ARG VITE_GATEWAY_URL=""
ARG VITE_CONTACT_EMAIL=""
ARG VITE_PRIVACY_EMAIL=""
ARG VITE_SECURITY_EMAIL=""
ARG VITE_SUPPORT_EMAIL=""
ENV VITE_SENTRY_DSN=$VITE_SENTRY_DSN \
    VITE_SENTRY_TRACES_SAMPLE_RATE=$VITE_SENTRY_TRACES_SAMPLE_RATE \
    VITE_MIXPANEL_TOKEN=$VITE_MIXPANEL_TOKEN \
    VITE_MIXPANEL_API_HOST=$VITE_MIXPANEL_API_HOST \
    VITE_RELEASE=$VITE_RELEASE \
    VITE_SITE_URL=$VITE_SITE_URL \
    VITE_GATEWAY_URL=$VITE_GATEWAY_URL \
    VITE_CONTACT_EMAIL=$VITE_CONTACT_EMAIL \
    VITE_PRIVACY_EMAIL=$VITE_PRIVACY_EMAIL \
    VITE_SECURITY_EMAIL=$VITE_SECURITY_EMAIL \
    VITE_SUPPORT_EMAIL=$VITE_SUPPORT_EMAIL
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Backend stage ---
FROM python:3.12-slim AS runtime

RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.create false

WORKDIR /app

# Install backend dependencies
COPY backend/pyproject.toml backend/poetry.lock ./
RUN poetry install --only main --no-root --no-interaction

# Copy backend source and install project
COPY backend/src/ ./src/
COPY backend/README.md ./README.md
RUN poetry install --only main --no-interaction

# Copy built frontend into backend static serving path
COPY --from=frontend-build /build/dist/ ./frontend-dist/

# Default config and data directories
RUN mkdir -p /app/config /app/data

# Store init configs outside the mount point so they survive bind/volume mounts
RUN mkdir -p /app/config-init
COPY backend/config/config.init.json /app/config-init/config.json
COPY backend/config/mcp.init.json /app/config-init/mcp.json
COPY backend/config/oauth_apps.init.json /app/config-init/oauth_apps.json

# Entrypoint script copies init configs if not already present
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

ENV MCPOLIS_HOST=0.0.0.0
ENV MCPOLIS_PORT=8080
ENV MCPOLIS_CONFIG_PATH=/app/config/config.json
ENV MCPOLIS_MCP_JSON_PATH=/app/config/mcp.json
ENV MCPOLIS_OAUTH_APPS_PATH=/app/config/oauth_apps.json
ENV MCPOLIS_DATA_DIR=/app/data
ENV MCPOLIS_AUDIT_LOG_PATH=/app/data/audit.jsonl
ENV MCPOLIS_DATABASE_URL=sqlite+aiosqlite:///./data/mcpolis.sqlite3
ENV MCPOLIS_FRONTEND_DIST=/app/frontend-dist

EXPOSE 8080

VOLUME ["/app/config", "/app/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
