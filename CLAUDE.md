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

## Glossary

- **MCP API key**: the key an admin gives an MCP whose Authentication is "None" (for example a GitHub access token); the app sends it on every call.

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

stdio MCPs run via the SandboxService boundary. Cloud mode never
falls back to the unsafe local-subprocess path on its own: it must be
named (`MCPOLIS_SANDBOX_PROVIDER=local-subprocess`, the
`.env.cloud.example` default), or the backend refuses to start. To
route stdio MCPs through E2B, set `MCPOLIS_E2B_API_KEY` in
`backend/.env.cloud` AND change its `MCPOLIS_SANDBOX_PROVIDER` line to
`e2b`: an explicit provider wins over the key, and `start.sh` keeps
the first line for a key, so edit the line rather than appending one.
Standalone mode still falls back to local-subprocess with a startup
warning.

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
  when debugging). Outputs `unit-junit.xml` + `unit-report.json`
  for grep-able pass/fail, in the run's own results folder (see
  [Where test results land](#where-test-results-land)).
- Integration tests (real-SDK, gated by `E2B_API_KEY`):
  `bash backend/run-integration-tests.sh [-j N] [args]` — `-j 4` by
  default. Same JUnit/JSON outputs (`integration-junit.xml`,
  `integration-report.json`) in its results folder.
- Standalone integration scripts: `bash backend/tests/integration/run-e2b-real-e2e.sh` (~$0.05, ~5 min) and `bash backend/tests/integration/run-list-orphan-sandboxes.sh`
- The paid tests share their E2B account with production. Every pytest
  session and every `e2b_real_e2e.py` run kills the sandboxes it
  created when it ends (passed, failed or Ctrl-C), and only those: the
  ones whose `mcpolis_instance` is `e2e-…` with the run's id as one
  part, or whose `test_run_id` is the run's id
  ([backend/tests/integration/_run_sandboxes.py](backend/tests/integration/_run_sandboxes.py)).
  The run prints `E2B cleanup for test run <id>: ...`;
  `run-list-orphan-sandboxes.sh --run-id <id>` lists what it left.
- E2E tests (Playwright, full-stack):
  `bash tests/run-e2e-tests.sh [--shards N] [spec...]`. The script
  is a thin wrapper around [tests/run-e2e-tests.py](tests/run-e2e-tests.py),
  a Python orchestrator. With `--shards N` it brings up N independent
  backend stacks on the 1xxxx port range — *preferred* bases backend
  `18080+i*10`, frontend `15173+i*10`, demo MCP `19999+i*10`, OAuth MCP
  `19998+i*10`, each probed upward for the first free port so a lone
  run lands on exactly these numbers but a run sharing the host (a
  leftover orphan, or a concurrent run) spills to the next free port
  instead of silently binding atop a squatter. Concurrent runs take
  turns at this: a run holds a host-wide lock
  (`/tmp/mcpolis-e2e-ports.lock`) from probing until its servers
  listen, because a probe can't see a port another run picked but
  hasn't bound yet (two runs started together failed their e2e legs in
  12 s, 2026-10-07). Seeding runs after the release. A waiting run
  prints the holder's pid and gives up after 10 minutes. Backed by an isolated
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
  `/tmp/mcpolis-e2e-spec-times.json` (refreshed after every run, and
  shared by every run on the host); cold-cache fallback is
  round-robin. Per-shard logs (`e2e-shard-N.log`), per-shard
  Playwright JSON (`e2e-shard-N.json`) and the aggregate
  (`e2e-aggregate.{json,txt}`) land in the run's results folder.
  Convention for splitting
  a spec: extract shared fixtures into `tests/e2e/_<feature>_helpers.ts`,
  break the file into `<NN><letter>-<slug>.spec.ts` siblings.

  **Loopback rule: 127.0.0.1, never `localhost`.** Every e2e server
  binds 127.0.0.1 and every e2e URL names it (`LOOPBACK_HOST` in the
  orchestrator). On the dev Mac, sustained bursts of new loopback TCP
  connections make every *new* loopback connection on the host stall
  1-8 s, and some fail with `ETIMEDOUT`; connections already open are
  unaffected (measured 2026-10-01). Those stalls were the `connect
  ECONNREFUSED ::1`, `connect ETIMEDOUT` and `ERR_SOCKET_NOT_CONNECTED`
  flakes under `make test-all`: the 4-shard e2e run held 130+ new
  connections/s for 6-12 s at a time and hit 3-6 stalls per run. Two
  causes: `localhost` doubled the attempts, because every client the
  tests use (Chromium, Playwright, Node fetch, httpx) tries the
  refused `::1` first, and the Vite proxy opened two new connections
  per API call until it got a pool (`backendAgent` in
  [frontend/vite.config.ts](frontend/vite.config.ts), guarded by
  `43-` and `44-*.spec.ts`). Since then the run stays under ~100/s
  (p95 63-92/s) with no stalls seen, but the margin is unknown: a
  synthetic churn test stalled at ~75/s held for over 10 s. Keep new
  e2e servers, fakes and URLs on 127.0.0.1, and keep their
  connections pooled.
- Frontend unit tests (vitest, jsdom): `bash frontend/run-unit-tests.sh [vitest args...]`.
  Outputs `vitest-junit.xml` + `vitest-report.json` (plus
  `frontend-build.log` on a no-arg run) for grep-able pass/fail, in
  its results folder. Mirror of the pytest wrapper. Plain
  `npm test` works too — the script just adds the JUnit/JSON
  reporters and the results folder.

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

### Run all three suites at once: `make test-all`

`make test-all` (→ [tests/run-all-tests.py](tests/run-all-tests.py))
runs the backend unit, full Playwright e2e, and E2B integration
suites **concurrently** and exits non-zero if any fails. The three
only oversubscribed the box when each ran at full tilt, so the
orchestrator bounds *total* concurrency to the host's cores rather
than serializing: each e2e shard counts ~2 CPUs, integration is
network-bound (~0), and unit's `-j` takes the rest with ~2 cores of
headroom. On a 14-core box that's `unit -j4`, `e2e --shards 4`,
`integration -j4`. Per-suite JSON reports are aggregated into
`all-aggregate.txt` and per-suite logs land at
`all-{unit,e2e,integration}.log`, all in test-all's results folder.
test-all hands that folder to each leg, so the legs' own files
(shard logs, JUnit XML) sit beside the summary.

Knobs (env vars): `NO_INTEGRATION=1` skips the paid E2B leg for
cheap local runs; `UNIT_JOBS` / `E2E_SHARDS` / `INTEGRATION_JOBS`
override the budget; `E2E_RETRIES` / `E2E_TIMEOUT_MS` (defaulted
to `3` / `45000` under `test-all`) are forwarded to Playwright,
which reads them in [tests/e2e/playwright.config.ts](tests/e2e/playwright.config.ts).

### Where test results land

Every runner above (and `run-e2b-broad-matrix.sh`) writes its logs
and reports into a results folder of its own, and prints it at the
start and as the last line:
`/tmp/mcpolis-test-runs/<date>-<time>-<suite>-<checkout>-<random>/`
(printed as its real path, `/private/tmp/...` on macOS).
`<suite>` is `all`, `unit`, `integration`, `e2e`, `vitest` or
`e2b-broad-matrix`; `<checkout>` is the worktree's folder name
(`mcpolis` for the main clone). Two runs at once, from one checkout
or several, never overwrite each other's results. Before, every run
shared fixed `/tmp/mcpolis-*` paths: on 2026-10-07 two worktrees ran
`make test-all` together, one run's e2e leg failed, and
`/tmp/mcpolis-all-e2e.log` already held the other run's green
output. The rule lives in [tests/run_folder.py](tests/run_folder.py).

- **Read the folder the run printed.**
  `/tmp/mcpolis-test-runs/latest-<suite>` points at the newest run of
  that suite, set when the run starts, so it is only right when no
  other run of that suite started since.
- `make test-all` passes its folder to every leg as
  `MCPOLIS_TEST_OUT_DIR`, so `latest-unit` / `latest-e2e` /
  `latest-integration` point there too. A leg that leaves no
  readable report fails test-all, even with exit code 0.
- Set `MCPOLIS_TEST_OUT_DIR` yourself to make a runner write into a
  folder you choose. It is yours: two runs given the same folder share
  it. A folder outside `/tmp/mcpolis-test-runs/` leaves the shared
  `latest-*` links alone.
- File names are the old fixed paths minus `/tmp/mcpolis-`
  (`unit-report.json`, `e2e-shard-0.log`, `all-aggregate.txt`, ...).
  Playwright's failure files (error context, traces) go to
  `e2e-shard-N-artifacts/` in the same folder, instead of the
  `tests/e2e/test-results/` that every run in a checkout shared. The
  runners no longer write the old paths, so any copies still in `/tmp`
  come from earlier runs or from checkouts on older code.
- A folder is deleted once it is older than a day AND 30 newer
  folders exist AND nothing in it changed for a day. A folder a
  `latest-*` link points at is kept.
- Shared on purpose: the e2e spec-times cache (written in one atomic
  step, so two runs finishing together can't tear it) and the e2e port
  lock above.

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
  boundary: the verifier mints a `ServiceAccessToken` with typed
  `role_name` / `org_id` (it is the only minter; a guard test checks),
  and the gateway controller passes `boundary_role` into the
  PolicyEngine calls. Never carry a role or org in scopes: OAuth scopes
  are client input (open client registration accepts any string). The
  gateway OAuth provider also refuses and strips the reserved
  `mcpolis:` scope namespace. A deleted role fails
  closed (zero tools). The controller reads the role from the bearer
  of the request being handled (`_request_auth_user`), not the
  session's: the MCP SDK keeps the auth of a session's `initialize`
  for the session's whole life, so a role rename left open token
  sessions on a role name that no longer exists.
- Tokens are pinned to one org. `ServiceTokenOrgPinMiddleware` resolves
  bare `/mcp` to the pinned org and 401s slug mismatches with the
  anti-enumeration body.
- Non-expiring + revocable; `last_used_at` updates are throttled to one
  write per minute per token.

User-facing doc: [docs/service-tokens.md](docs/service-tokens.md).

## Admin actions run to completion

An admin action writes several stores in a row (saved config, running
policy, token registry, membership rows, audit log); cut half-way it
leaves them disagreeing. So every admin action that changes something
runs to its end once started, whatever cancels the request:
`finish_despite_cancels` / `@runs_to_completion` in
[backend/src/mcpolis/domain/services/cancel_shield.py](backend/src/mcpolis/domain/services/cancel_shield.py),
on the shared actions (`UserAdminService`, `UpstreamAdminService`,
`RoleAdminService`) that both doors call. It runs the action in a task
of its own, so neither an anyio scope cancel (the MCP SDK on a client's
`notifications/cancelled`, `BaseHTTPMiddleware`) nor a native
`Task.cancel()` (uvicorn at the end of its graceful shutdown) cuts it,
then passes the cancel on.

- The one `tools/call` wrapper of the Admin MCP and the operator MCP
  (`install_call_tool_wrapper` in `admin_tool_calls.py`) runs every tool
  not annotated read-only that way. Don't add a
  per-tool `anyio.CancelScope(shield=True)`: a handler that returns
  normally after the client cancelled makes the SDK answer twice
  ("Request already responded to"), which kills the whole session.
- Dashboard and operator writes (every `/api/` request but GET, HEAD and
  OPTIONS) run to completion too, through `RunToCompletionMiddleware`
  (the outermost middleware). The shutdown drains every held job set
  (`drain_every_set`): it waits, within a bound, for the jobs each
  `BackgroundTaskSet` holds, then closes the stores while every set
  refuses new ones (`refusing_new_jobs`).
- The same helper carries every other piece of work a cancel must not
  cut: the gateway's audit write, the E2B sandbox kill, a connect
  letting go of its transport, a token refresh. Where they differ, it
  is an option (`held_by`, `time_limit`, `on_failure`, `pass_cancel_on`,
  `wait_after_cancel`). Add an option there rather than hand-roll
  another shielded wait, so a fix lands once.
- Test a cancel over the real protocol (`cancel_mcp_call_while_gated` in
  `tests/unit/factories.py`) and against a native cancel
  (`cancel_natively_while_gated`). `cancel_while_gated` alone skips the
  SDK's answer step.

## Request rate limits

Sliding one-minute windows, decided by `RateLimitService`
([backend/src/mcpolis/domain/services/rate_limit_service.py](backend/src/mcpolis/domain/services/rate_limit_service.py))
over the `RateLimiter` port: in-memory in standalone, one Redis Lua
script in cloud.

| Surface | Charged to | Enforced in | Caller sees |
|---|---|---|---|
| Gateway `tools/call`, allowed | the caller in the org, and the whole org | gateway controller (`_admit_call`) | `isError` tool result naming the wait |
| Gateway `tools/call`, `resources/read`, `prompts/get`, refused | the caller only (`tool_call:denied:<caller>`, a service token as `svc:<label>@<org>`) | `_admit_call`, `_refused`, `_refused_text`, `_refuse_disabled_mcp` | same, or the read's / prompt's text |
| Admin MCP tool calls | the admin | low-level `tools/call` wrapper (`install_call_tool_wrapper`) | `isError` tool result |
| Dashboard `/api/*` | the signed-in user, else the client IP | `RateLimitMiddleware` | HTTP 429 + `Retry-After` |
| Sign-in endpoints | the client IP, one bucket per `SignInGroup` | `RateLimitMiddleware` | HTTP 429 + `Retry-After` |

The numbers live in two places only: per-plan tool-call limits in
`PlanLimits` ([plan_policy.py](backend/src/mcpolis/domain/services/plan_policy.py)),
the rest in `Settings` (`MCPOLIS_RATE_LIMIT_*`). They are runaway
ceilings, not the Terms §3 fair-use line, and the user docs
deliberately don't publish them.

Rules to keep when touching this area:

- A check charges all of its buckets or none. A refusal by the
  caller's own bucket must not spend the org bucket, or one runaway
  agent drains its teammates' quota.
- Only calls the org's policy allows reach the org bucket. Every
  refusal path of `tools/call`, `resources/read` and `prompts/get`
  (denied by policy, naming an org the caller isn't in, an unknown
  tool, prompt or resource) goes through `_admit_call`, `_refused`,
  `_refused_text` or `_refuse_disabled_mcp`: charged to the caller's
  refused-call bucket, one bucket for all three, held to the Free
  per-caller limit with no plan lookup. Any signed-in account can
  reach `/mcp/{slug}` (membership there is enforced by policy), so no
  refusal may spend, or reveal the plan of, someone else's org. A
  caller over that limit gets the refusal before any `denied` audit
  row is written (`_charge_and_audit_denial`, shared by the three).
  Reads and prompts check access before existence: an MCP the org
  doesn't have gets the same "disabled for you" answer, so the answer
  can't list an org's MCPs. A service token's refused-call bucket is
  per org (labels are unique per org only); a person's is one bucket
  across every org.
- The Admin MCP limit is charged to `current_caller_id()`, the bearer
  identity. `current_user_id` alone is the dashboard cookie and stays
  "anonymous" for AI clients; an earlier version keyed on it and put
  every admin of every org in one bucket. The guard is the two-admin
  real-transport test in `test_rate_limit_gateway.py`.
- Tool calls are refused with a tool error, never an HTTP 429: AI
  clients read the error and wait; many treat a 429 on the MCP
  transport as a broken connection.
- On the MCP OAuth endpoints a 429 carries `{"error":
  "too_many_requests"}`: the MCP SDK knows that code and keeps its saved
  sign-in. An unknown code makes it drop a refused token refresh and
  restart the interactive sign-in, refused by the same bucket.
- Sign-in endpoints are bucketed per `SignInGroup` so the dashboard's
  automatic error reports can't close MCP token refresh behind the
  same IP. The two EventSource endpoints (`/api/events`, upstream log
  stream) are never limited: a browser never retries an EventSource
  that got a non-200 answer.
- The client IP is the `X-Forwarded-For` entry
  `MCPOLIS_TRUSTED_PROXY_HOPS` places from the right; never the
  leftmost one, which the client writes. Compose sets 1 behind the
  bundled nginx and 2 with `docker-compose.proxied.yml`. A private
  (non-loopback) result logs `rate_limit.client_ip.private` once per
  process: behind proxies it means the hop count is too low and every
  client shares one bucket. IPv6 is keyed per /64; `::ffff:a.b.c.d` as
  its IPv4 address.
- A failure of the limiting machinery admits the request: rate limiting
  must never be what takes the gateway down. Each Redis check is capped
  at 0.5 s (a Redis that never answers costs a bounded delay, not a
  hang); the outage logs one `rate_limit.check.failed_open` ERROR per
  minute and `rate_limit.check.recovered` when it ends. A plan lookup
  failure admits too; plans are cached 30 s.
- Refusals log `rate_limit.exceeded` (and track `rate_limit_hit` when
  the caller is known) once per bucket per window via `EmitThrottle`;
  counts swallowed in between ride on the next line, or on a `closing`
  line when the bucket goes quiet. A quiet bucket is forgotten either
  way, so a caller rotating client IPs can't grow the reporter.
- `MCPOLIS_RATE_LIMIT_ENABLED=false` switches every limit off with a
  restart, no deploy.
- E2E lifts the per-IP and per-user limits (all its traffic comes from
  127.0.0.1, with shared seeded users, in one Redis); tool-call limits
  keep their plan values and `47-rate-limits.spec.ts` asserts them on
  the `/mcp/{slug}/` URL, per caller and per org.

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
- Empty value requires `MCPOLIS_E2B_API_KEY` and then means `e2b`.
  With no key, cloud mode refuses to start: there is no silent
  fallback to the unsandboxed `local-subprocess` runner (operator
  decision, 2026-10-07). Standalone mode keeps that fallback, with a
  startup warning.
- `MCPOLIS_SANDBOX_PROVIDER=local-subprocess` (no isolation) is
  accepted only when named explicitly AND `MCPOLIS_HOST` is a literal
  loopback address: local dev (`.env.cloud.example` names it) and
  the e2e runner. Any other bind is rejected; the production image
  binds `0.0.0.0`.
- `MCPOLIS_SANDBOX_PROVIDER=own-runner` is rejected outright
  (legacy backend, removed).

### Waking a paused sandbox never reuses its MCP process

E2B pauses an idle sandbox (`MCPOLIS_E2B_IDLE_PAUSE_SECONDS`,
default 60s) and resumes it on the next call. On resume the
service **reuses the sandbox and replaces the MCP process**:
`connect_sandbox` → `kill_command(old_pid)` → a fresh
`run_command` → a fresh MCP `initialize`. It does not reattach
via `connect_command`.

Reattaching is what the service used to do, and it caused two
production bugs. A process resumed from a snapshot still believes
it owns every TCP connection its HTTP client had pooled; those
were severed while it slept. It writes into them and gets
`ECONNRESET`, producing exactly one opaque tool failure per pooled
socket on the first calls after a wake. Reproduced 20 of 20 wakes,
failure count tracking pool size exactly, by
[backend/tests/integration/diagnose_wake_network.py](backend/tests/integration/diagnose_wake_network.py)
(run it via `bash backend/tests/integration/run-wake-network.sh`).
The same reuse carries envd's fan-out wedge, which is the
silent-stdout stall.

Consequences to keep in mind when touching this area:

- The sandbox is deliberately KEPT on a wake. Nearly all of a cold
  start is downloading the MCP's package (7-22s per server in
  production, against ~3s for one already on disk), and that cache
  lives on the sandbox filesystem. Keeping it takes TWO things:
  the reopen leaves the persisted ref in place AND calls
  `preserve_sessions_for_upstream` first. That call lives in
  `_open_shared`, which every shared reopen funnels through (wake,
  heal, Start, boot); a copy at a call site was orphaned once
  already. Leaving the ref
  alone is not enough on its own, because the heal's close-then-open
  tears the old session down with `preserve=False`, which deletes
  the ref and kills the sandbox before the reopen can read it. An
  independent review caught that; the regression gate is
  `test_heal_asks_to_preserve_the_sandbox_before_reopening`.
- **There is no retry exception for wakes, and adding one is a
  mistake we have already made twice.** The watcher sets
  `transport_failed` the moment the output stream ends, which for a
  paused sandbox is the pause itself (measured 8.7s before the next
  request arrived). `ensure_shared_connected` then refuses that
  session inside `_resolve_session`, before the gateway writes
  anything, so the request lands on a rebuilt session and a fresh
  process. Nothing is lost, so nothing needs re-sending.
  An earlier design let the request reach the dead session and then
  tried to prove re-sending was safe. Two independent review passes
  each found a different way for an already-delivered request to
  claim that proof. If you find yourself reintroducing a
  "this one is safe to repeat" flag, the ordering above has broken.
- The pid changes on every wake, so the persisted ref must be
  re-written. Don't reintroduce a "ref is still valid, skip the
  persist" shortcut.
- `set_timeout` must still be re-applied after any resume: E2B
  resets the idle window to its own 300s default on `auto_resume`.
- E2B's window is NOT an idle timer: it runs from the create or the
  last `set_timeout`, and traffic does not reset it (measured
  2026-10-01). `IdlePauseTimer` makes it one by re-arming it on
  CALLER traffic only: a request in `COUNTED_METHODS` (initialize,
  tools/call, resources/read, prompts/get, completion/complete), the
  answer to one, and every ~idle/3 while one is unanswered (capped at
  300s). Without it a busy sandbox paused every 60s and cut off
  running calls. The MCP program is the customer's own code, so
  nothing it causes may count: its notifications, answers to no
  request, the list requests its `list_changed` notices trigger, or
  the gateway's pings. Counting any of them is the keep-alive Terms §3
  forbids. Answer ids are matched the way the MCP client matches them
  (text "7" answers request 7), and a request is recorded BEFORE it is
  written: a fast answer can arrive before `send_stdin` returns.
- The pump's wake branch is a BACKSTOP, not the main path: it
  catches the race (a dispatch that passed the liveness gate just
  before the watcher fired) and the case where E2B goes quiet
  instead of severing, so the watcher never fires. It never writes
  the frame; the caller eats one error and the manager rebuilds.
- A wake APPLIES pending edits to **command, args, env and Sandbox
  files**, because the replacement process picks them up fresh, and
  `connect_shared` re-persists `started_config_hash`, so the
  dirty-config banner clears itself. Deliberate.
  **CPU and RAM are different**: size is baked into the E2B template
  at create time and a reconnect attaches by id, so it cannot
  re-size. `_try_reconnect` therefore compares the requested template
  against `metadata["e2b_template"]` on the ref and falls through to
  fresh-create on a mismatch. Without that check the banner would
  clear while the MCP kept running at the old size — a dashboard
  asserting something false, found in the fourth review pass.

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
