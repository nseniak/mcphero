import { defineConfig } from "@playwright/test";

// Default to the historic single-shard ports so a stale invocation
// without env vars still hits the same backend a developer is used
// to. The Python orchestrator (tests/run-e2e-tests.py) overrides
// these per-shard via E2E_BASE_URL.
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:5173";

// Per-test timeout and retry count are env-tunable so the cross-suite
// orchestrator (tests/run-all-tests.py) can loosen them when e2e shares
// the box with the unit + integration suites (``make test-all``), without
// changing what a standalone ``run-e2e-tests.sh`` run sees.
const testTimeout = Number(process.env.E2E_TIMEOUT_MS ?? 30_000);
const retries = Number(process.env.E2E_RETRIES ?? 1);

export default defineConfig({
  testDir: ".",
  timeout: testTimeout,
  // Retries absorb transient connection-layer flakes under parallel
  // multi-shard load — e.g. ``page.goto``/``page.reload`` hitting
  // ``net::ERR_SOCKET_NOT_CONNECTED`` when a shard's server briefly drops
  // the connection. These are infra blips, not behavior failures, so a
  // retry reclassifies them as "flaky" in the report instead of failing the
  // whole gate. A genuinely broken spec still fails every try. Defaults to
  // 1; ``make test-all`` raises it (E2E_RETRIES=2) so a *double* blip —
  // both attempts starved at once, the lone hard-fail mode seen under the
  // heaviest cross-suite load — still recovers.
  retries,
  // ``oauth_test_mcp_server.py`` keeps token / TTL / queued-email
  // state in module globals; specs reset that state in ``beforeEach``
  // but two specs running in parallel still trample each other's
  // resets. Pinning workers=1 inside a shard keeps the fake's global
  // state safe — the Python orchestrator gets parallelism by spawning
  // N independent shards, each with their own fake on a different
  // port, instead of relaxing this within-shard.
  workers: 1,
  use: {
    baseURL,
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
    {
      name: "watch",
      use: {
        browserName: "chromium",
        headless: false,
        launchOptions: { slowMo: 800 },
      },
    },
  ],
});
