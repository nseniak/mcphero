# Contributing to MCP Hero

Thanks for your interest in improving MCP Hero. This guide covers how to
set up a dev environment, run the tests, and submit a change.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues go through [SECURITY.md](SECURITY.md), not public issues.

## Getting set up

Start with the "Develop from source" section of the [README](README.md):
clone the repo, set up a Python 3.12 environment for the backend, install
the frontend deps, and run the stack with `bash start.sh` (cloud mode) or
`bash start.sh standalone`.

[CLAUDE.md](CLAUDE.md) is the developer guide: it documents the start /
stop / restart scripts, the test runners, the standalone-vs-cloud
switches, and the sandbox-provider rules. It's worth a read before your
first change.

## Naming: codename vs brand

The project has two names and uses each deliberately (see CLAUDE.md):

- **`mcpolis`** is the internal codename. Use it for every technical
  artifact: module and class names, env vars, container and DB names,
  file paths.
- **`MCP Hero`** is the user-facing brand. Use it only where users see
  it: UI strings, marketing copy, page titles.

Rule of thumb: if renaming the product tomorrow would force the change,
it's user-facing (brand); otherwise it's technical (codename).

## Running the tests

| Suite | Command |
|-------|---------|
| Backend unit (pytest) | `bash backend/run-unit-tests.sh` |
| Backend type check (pyright) | `bash backend/run-pyright.sh src/ tests/` |
| Frontend unit (vitest) | `bash frontend/run-unit-tests.sh` |
| End-to-end (Playwright, full stack) | `bash tests/run-e2e-tests.sh` |

Integration tests (`bash backend/run-integration-tests.sh`) exercise the
real E2B sandbox SDK and are gated behind an `E2B_API_KEY`; they
auto-skip without one, so you don't need an E2B account to contribute.

**Before opening a PR**, get backend unit + frontend unit + e2e green and
pyright clean on `src/`. CI and reviewers will expect the same.

## Code conventions

- **Typed Python.** Annotate everything; prefer precise types over
  `Any`; model structured data with Pydantic, not bare dicts.
- **Clean architecture.** Keep adapters, domain (model + services), and
  entrypoints (routes + controllers) separated as the existing tree does.
- **Tests:** top-level test functions (no test classes, no pytest
  fixtures). Factor shared setup into `make_*` helper functions and call
  them explicitly. Prefer dependency injection over patching.
- **DRY:** reuse existing components; factor out shared logic rather than
  copy-pasting.
- **Commits:** Conventional Commits style (`feat:`, `fix:`, `docs:`,
  `refactor:`, `chore:`, `test:`), matching the existing history.

## Submitting a change

1. Fork the repo and branch off `main`.
2. Make your change with tests; keep the diff focused.
3. Run the gates above and make sure they pass.
4. Open a pull request describing the change and the motivation. Link any
   related issue.

Maintainers review and integrate accepted pull requests. We may ask for
adjustments, and we squash or reshape commits as needed when merging.

## License of contributions

MCP Hero is licensed under **AGPL-3.0-or-later** (see [LICENSE](LICENSE)).
By submitting a contribution, you agree that it is licensed under the same
terms (inbound = outbound). Only contribute code you have the right to
license this way.
