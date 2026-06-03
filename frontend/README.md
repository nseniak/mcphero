# Frontend (MCP Hero dashboard)

The admin dashboard and marketing site for MCP Hero, built with React +
TypeScript + Vite (Tailwind for styling). It talks to the backend
gateway's HTTP API to manage organizations, MCP upstreams, users, roles,
and access policies, and to watch live connection/event state.

## Develop

From the repo root, `bash start.sh` brings up the backend and this dev
server together — see the [root README](../README.md) and
[CLAUDE.md](../CLAUDE.md). To run only the frontend dev server:

```bash
cd frontend
npm install
npm run dev        # Vite dev server with HMR on http://localhost:5173
```

The dev server proxies `/api` and `/mcp` to the backend on
`http://localhost:8080`; override with `MCPOLIS_BACKEND_HOST` /
`MCPOLIS_BACKEND_PORT`. When serving behind a proxy or tunnel, set
`MCPOLIS_DEV_ALLOWED_HOSTS` to the external host(s), comma-separated.

## Commands

```bash
npm run dev        # dev server (HMR)
npm run build      # type-check (tsc -b) + production build
npm run preview    # serve the production build locally
npm run lint       # ESLint
npm test           # vitest (jsdom)  — or: bash run-unit-tests.sh
```

`bash run-unit-tests.sh` is the wrapper used in CI: it runs vitest with
JUnit/JSON reporters written to `/tmp/mcpolis-vitest-*`.

## Layout

- `src/pages/` — dashboard and marketing pages
- `src/components/` — shared UI components
- `src/hooks/` — data-fetching and state hooks
- `src/lib/` — client utilities (API client, secret detection, …)

See [CONTRIBUTING.md](../CONTRIBUTING.md) for conventions and the full
test matrix.
