# E2B template grid

mcpolis publishes a fixed grid of [E2B](https://e2b.dev) templates, one
per `(language × cpu × ram)` combination. The
`E2BSandboxService.session()` resolves an upstream's
`(command, SandboxResources)` to a template name and calls
`Sandbox.create(template=<name>, ...)`. Off-grid combinations are
rejected upfront by `validate_resources()`; this directory exists to
guarantee every grid combination has a real published template
behind it.

## Grid

- **Languages:** `node` (npx-distributed MCPs), `python` (uvx-distributed),
  `docker` (`docker run`-distributed MCPs; the template starts `dockerd`
  at boot via `set_start_cmd` so the MCP's `docker run -i …` has a live
  daemon)
- **CPU / RAM pairings (per language):**

  | vCPU | RAM (MiB)            |
  |------|----------------------|
  | 1    | 1024, 2048           |
  | 2    | 2048, 4096           |
  | 4    | 4096, 8192           |
  | 8    | 4096, 8192           |

- **Total:** 24 templates (8 pairings × 3 languages)

RAM scales with CPU rather than a full cross-product so we don't
burn build time on unbalanced combos no admin is going to pick
(e.g. 1 vCPU + 8 GiB).

> **Docker sizing note.** E2B's documented floor for running Docker is
> 2 vCPU / 2 GB; `dockerd` alone costs ~150–250 MB. We still publish the
> two 1-vCPU docker templates (`mcpolis-docker-cpu1-ram1024`,
> `mcpolis-docker-cpu1-ram2048`) so the grid stays a clean cross-product,
> but the dashboard shows a non-blocking warning when `docker` is paired
> with a 1-vCPU size, and real images will likely OOM there.

Naming convention: `mcpolis-{lang}-cpu{cpu}-ram{ram}`. Example:
`mcpolis-node-cpu2-ram4096`.

## Layout

```
runner/e2b-templates/
├── README.md                    (this file)
├── Dockerfile.node              shared Dockerfile for every node template
├── Dockerfile.python            shared Dockerfile for every python template
├── Dockerfile.docker            shared Dockerfile for every docker (dind) template
├── Makefile                     `make build`, `make check`, `make list`, …
└── build_grid.py                v2 Template SDK driver; matrix lives here
```

`build_grid.py` is the operator-side source of truth for the
matrix. A consistency test
(`backend/tests/unit/test_e2b_template_grid.py::test_driver_matches_backend_template_grid`)
locks it to the backend's
`mcpolis.adapters.sandbox_e2b.template_grid` constants so drift
between the two surfaces fails CI rather than silently shipping
broken UI.

## Build mechanism

```bash
# one-time: install the e2b CLI + authenticate
npm i -g @e2b/cli
e2b auth login

# (re)build the entire grid via the v2 Template SDK
make build         # talks to E2B; ~8-12 min for the full 24-combo grid
make check         # CI lint: validate matrix + Dockerfile presence (no API)
make list-names    # print the 24 names build_grid.py would publish
make list          # query E2B; show mcpolis-* templates currently published
```

`make build` is idempotent — re-publishing under the same name
overwrites the previous build server-side.

## Adding a new combo

1. Edit `build_grid.py` and append the new `(cpu, ram)` to
   `CPU_RAM_PAIRS`.
2. Edit `backend/src/mcpolis/adapters/sandbox_e2b/template_grid.py`
   and append the same pair (the consistency test guards drift).
3. `make check` to validate; then `make build` to publish.
4. Bump the docs in `internal/documents/e2b-backend-setup.md`.

Removing a combo: same flow, but also delete the published template
on the E2B dashboard once no upstream references it.

## Why a fixed grid (not on-demand builds)

E2B templates lock CPU/RAM at build time, so per-sandbox sizing
requires one template per combo. v1 deliberately skips on-demand
builds:

- Spawn-time stays deterministic — `Sandbox.create()` doesn't pay a
  multi-minute build cost.
- The set of published templates stays bounded; no GC story needed
  for short-lived experimental templates.
- Admins who want a combo we don't have either pick the closest
  valid one or open a PR to extend the grid (single-line matrix
  edit).
