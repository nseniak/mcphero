# Development

## Naming: codename vs user-facing brand

This project has two names. Use the right one in the right place.

- **`mcpolis`** is the internal **codename**. It's stable and won't change.
  Use it for every technical artifact: file and folder names, Python and
  TypeScript module/class names, database and collection names, conda
  envs, Docker images and volumes, skill files, memory note filenames,
  log channels, env vars, anything that ships in the repo or
  infrastructure.
- **`MCP Hero`** is the **user-facing brand**. The brand may change. Use
  it only where users see it: marketing copy, dashboard UI strings,
  `<title>` and SEO metadata, the prose inside admin docs, the JSON-LD
  publisher entries, billing emails. Don't bake the brand into anything
  the code reads or writes.

When in doubt, ask: "if marketing renamed the product tomorrow, would
this need to change?" If yes, it's user-facing; use `MCP Hero`. If no,
it's technical; use `mcpolis`.

## General rules

This is a young project with no backward-compatibility guarantees on
persistence formats or APIs. Don't add migration or compatibility
shims for old data/config formats unless explicitly asked — prefer the
clean change.

Multiple agent sessions may work in this repo concurrently, and they
share the git index. Before staging anything, check that
`git diff --cached` is empty — staged content you didn't put there
belongs to another session; stop and tell the operator instead of
committing or unstaging it. Stage and commit as one uninterrupted
step (no gates or long commands between `git add` and `git commit`),
and re-verify `git log -1` immediately before amending or rewriting
anything. (Learned 2026-06-12: two sessions raced the index and
produced a union commit with a lost hunk; it took three repairs to
untangle.)

When a flow depends on the OAuth callback origin (or any other
host-aware code path), drive Chrome / Playwright against the same
origin the app is actually served on, so those paths see what a real
user would.

## Starting services

- `bash start.sh` — **cloud mode** (default). Auto-starts Docker Desktop
  if needed, brings up the `dev` compose profile (mongo + redis), loads
  dev secrets from `backend/.env.cloud` (auto-created from
  `.env.cloud.example` on first run), then starts the backend against
  the `mcpolis_dev` Mongo database. Containers are left running on
  exit; the next boot is fast. Rotate the dev secrets by editing
  `backend/.env.cloud` (gitignored).
- `bash start.sh standalone` — **standalone mode**. File-backed storage
  under `backend/config/` + `backend/data/`. No containers required.

Optional flags (any position — the script accepts them with or
without an explicit mode):

- `--fake-auth` — skip Google OAuth: enables the dev-stub dashboard
  email picker AND the gateway's test-bearer-token endpoint. Dev only.
- `--no-demo` — skip mounting the bundled demo MCP server. By default
  the "kitchen sink" demo MCP server is mounted at `/dev/mcp-demo` and
  auto-registered as a service-account upstream on the default org
  (standalone) or the first org (cloud). Exposes five widget kinds
  (inline / fullscreen / pip / counter / solar) so MCP-Apps widget
  plumbing can be smoke-tested through the gateway.

stdio MCPs run via the SandboxService boundary. Set
`MCPOLIS_E2B_API_KEY` in `backend/.env.cloud` to route them through
E2B. Without an API key the backend falls back to the unsafe
local-subprocess path with a clear warning at startup.

`bash stop.sh` tears down backend + frontend; pass `--all` to also
stop the cloud-mode mongo + redis containers.

`bash restart.sh` runs `stop.sh` then `start.sh` (forwards any
flags). This is the script agents must run at the end of any
coding sequence that produces something the operator could
exercise locally — UI tweaks, route changes, anything visible
through the dashboard or gateway. Skip it only when the change
is invisible to a running stack (test-only edits, docs, lint
config, dead-code removal). Rationale: a green CI run proves
correctness on paper; a fresh local boot proves the operator can
actually see the change without hand-restarting. Forward the same
flags the operator would use (default cloud; pass `standalone`
or `--fake-auth` if the change calls for it).

Both modes serve the same ports:

- Backend: http://localhost:8080 (log: /tmp/mcpolis-backend.log)
- Frontend: http://localhost:5173 (log: /tmp/mcpolis-frontend.log)

## Python environment

- Conda env: `mcpolis` (source miniforge3 conda.sh first)
- Install all binaries (pip, npm, system tools, etc.) inside the
  `mcpolis` conda env — never into the base env or globally.
- Unit tests: `bash backend/run-unit-tests.sh [-j N] [pytest args...]`
  Parallel via pytest-xdist (`-j auto` by default; `-j 1` for serial
  when debugging). Outputs `/tmp/mcpolis-unit-junit.xml` +
  `/tmp/mcpolis-unit-report.json` for grep-able pass/fail.
- Integration tests (real-SDK, gated by `E2B_API_KEY`):
  `bash backend/run-integration-tests.sh [-j N] [args]` — `-j 4` by
  default. Same JUnit/JSON outputs under `/tmp/mcpolis-integration-*`.
- Standalone integration scripts: `bash backend/tests/integration/run-e2b-real-e2e.sh` (~$0.05, ~5 min) and `bash backend/tests/integration/run-list-orphan-sandboxes.sh`
- E2E tests (Playwright, full-stack):
  `bash tests/run-e2e-tests.sh [--shards N] [spec...]`. The script
  is a thin wrapper around [tests/run-e2e-tests.py](tests/run-e2e-tests.py),
  a Python orchestrator. With `--shards N` it brings up N independent
  backend stacks on the 1xxxx port range — *preferred* bases backend
  `18080+i*10`, frontend `15173+i*10`, demo MCP `19999+i*10`, OAuth MCP
  `19998+i*10`, each probed upward for the first free port so a lone
  run lands on exactly these numbers but a run sharing the host (a
  leftover orphan, or a concurrent run) spills to the next free port
  instead of silently binding atop a squatter. Backed by an isolated
  test mongo on `27018` and test redis on `6380` (compose `test`
  profile, started on demand, `--clean` to tear down on exit). Each
  run's Mongo databases carry a per-run token (`mcpolis_e2e_<token>_sN`)
  so two concurrent runs don't trample each other; Redis state is
  org-id-scoped (each run's seeded org gets a fresh UUID). Before
  bootstrapping, the orchestrator reaps leaked e2e child processes
  (orphans from an interrupted prior run squatting an in-band port —
  never the dev stack or a live concurrent run). Specs are partitioned
  across
  shards by a longest-processing-time-first bin-packer that reads
  `/tmp/mcpolis-e2e-spec-times.json` (refreshed after every run);
  cold-cache fallback is round-robin. Per-shard logs at
  `/tmp/mcpolis-e2e-shard-N.log`; per-shard Playwright JSON at
  `/tmp/mcpolis-e2e-shard-N.json`; aggregate at
  `/tmp/mcpolis-e2e-aggregate.{json,txt}`. Convention for splitting
  a spec: extract shared fixtures into `tests/e2e/_<feature>_helpers.ts`,
  break the file into `<NN><letter>-<slug>.spec.ts` siblings.
- Frontend unit tests (vitest, jsdom): `bash frontend/run-unit-tests.sh [vitest args...]`.
  Outputs `/tmp/mcpolis-vitest-junit.xml` + `/tmp/mcpolis-vitest-report.json`
  for grep-able pass/fail. Mirror of the pytest wrapper. Plain
  `npm test` works too — the script just adds the JUnit/JSON
  reporters and the `/tmp/` cleanup.

All four runners above are safe to execute while `bash start.sh`
is up — the dev session, dev Mongo (`mcpolis_dev` on `:27017`),
and dev Redis are never touched:

- **E2E** uses the compose `test` profile (mongo `:27018`, redis
  `:6380`) and a backend stack on the 1xxxx port range.
- **Backend unit (pytest)** shares the dev Mongo daemon on
  `:27017` but every test creates a throwaway
  `mcpolis_test_<uuid>` database and drops it on teardown
  ([backend/tests/unit/mongo_fixture.py](backend/tests/unit/mongo_fixture.py)).
- **Frontend vitest** is pure jsdom, no network.
- **Integration** hits hosted E2B, no local infra.

If pytest ever contends with dev on the shared Mongo daemon,
export `MCPOLIS_TEST_MONGO_URI=mongodb://localhost:27018` to
point it at the e2e test mongo instead (running whenever an
e2e run hasn't been torn down with `--clean`).
- Type check: `bash backend/run-pyright.sh src/ tests/`

## Service tokens (gateway auth for headless agents)

Non-interactive bearer credentials for the `/mcp` gateway: `svct_`-prefixed
random secrets, sha256-hashed in the `service_tokens` registry
(file-backed in standalone, plain Mongo collection in cloud — nothing
secret at rest). Minted/revoked by org admins via
`/api/admin/service-tokens` and the dashboard's Service Tokens page; the
raw value is returned exactly once at mint.

Key invariants:

- The gateway's `BearerAuthBackend` wraps a composite verifier
  (`adapters/auth/service_token_verifier.py`): `svct_` bearers go to the
  registry, everything else to the OAuth provider. `/admin-mcp` keeps the
  raw OAuth provider, so service tokens are structurally rejected there.
- Identity is `svc:<label>` — **never** an entry in `config.users`, never
  on the Team page, never a plan seat. The role is resolved at the auth
  boundary: the verifier puts `mcpolis:role:<role>` / `mcpolis:org:<org>`
  scopes on the AccessToken, and the gateway controller passes
  `boundary_role` into the PolicyEngine calls. A deleted role fails
  closed (zero tools).
- Tokens are pinned to one org. `ServiceTokenOrgPinMiddleware` resolves
  bare `/mcp` to the pinned org and 401s slug mismatches with the
  anti-enumeration body.
- Non-expiring + revocable; `last_used_at` updates are throttled to one
  write per minute per token.

User-facing doc: [docs/service-tokens.md](docs/service-tokens.md).

## Sandbox provider selection

stdio MCPs run behind a `SandboxService` boundary with two backends:
`e2b` (hosted, the production default) and `local-subprocess` (no
isolation, dev-only). The active backend is picked at startup via
`MCPOLIS_SANDBOX_PROVIDER` and resolved per-org via
`SandboxResolver` (today the resolver returns the global default;
per-org override is a half-day swap when `Org.sandbox_provider`
ships).

Cloud-mode rules enforced by `validate_startup_secrets` in
[backend/src/mcpolis/entrypoints/config.py](backend/src/mcpolis/entrypoints/config.py):

- `MCPOLIS_SANDBOX_PROVIDER=e2b` requires `MCPOLIS_E2B_API_KEY`.
- `MCPOLIS_SANDBOX_PROVIDER=local-subprocess` is rejected outright
  (no-isolation path; dev-only).
- `MCPOLIS_SANDBOX_PROVIDER=own-runner` is rejected outright
  (legacy backend, removed).
- Empty value falls back to `e2b` when an API key is set, else
  `local-subprocess` with a startup warning.

The 24-template grid (node / python / docker × 8 CPU/RAM pairs) is in
[runner/e2b-templates/](runner/e2b-templates/); on docker templates,
`command: docker` MCPs (`docker run -i …`) get a live daemon from
`E2BSandboxService._start_docker_daemon`, which adopts the systemd-managed
`dockerd` the image boots with (or stops it and launches its own —
never both: a second dockerd dies on the volume-store flock and unlinks
the socket path). `set_start_cmd` can't be used for this; see the note
in [runner/e2b-templates/build_grid.py](runner/e2b-templates/build_grid.py).
Rebuild with
`cd runner/e2b-templates && make build` after any matrix edit, and keep
[backend/src/mcpolis/adapters/sandbox_e2b/template_grid.py](backend/src/mcpolis/adapters/sandbox_e2b/template_grid.py)
in sync (tests/test_e2b_template_grid.py guards drift).
