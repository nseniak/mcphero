"""Build the mcpolis E2B template grid via the v2 Template SDK.

Single async-driver script that talks to E2B's modern Template
SDK (``e2b.AsyncTemplate``) to publish the 24-template grid. The
matrix below is the operator-side source of truth; a consistency
test in ``backend/tests/unit/test_e2b_template_grid.py`` enforces that
the backend's
``mcpolis.adapters.sandbox_e2b.template_grid.CPU_RAM_PAIRS`` matches.

Usage::

    # Cutover (~5-8 min):
    cd runner/e2b-templates
    make build

    # CI lint (no API calls):
    make check
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "runner" / "e2b-templates"

# Matrix — kept in lock-step with the backend's
# ``CPU_RAM_PAIRS`` / ``LANGUAGES`` constants by
# ``test_e2b_template_grid.py``.
LANGUAGES: tuple[str, ...] = ("node", "python", "docker")

# Valid (cpu, ram_mb) pairings — RAM scales with CPU rather than a
# full cross-product so we don't burn template-build time on
# unbalanced combos no admin would pick (e.g. 1 vCPU + 8 GiB).
# Total templates = ``len(LANGUAGES) * len(CPU_RAM_PAIRS) = 24``.
CPU_RAM_PAIRS: tuple[tuple[int, int], ...] = (
    (1, 1024),
    (1, 2048),
    (2, 2048),
    (2, 4096),
    (4, 4096),
    (4, 8192),
    (8, 4096),
    (8, 8192),
)


@dataclass(frozen=True)
class Combo:
    """One (language × cpu × ram) cell from the matrix."""

    language: str
    cpu: int
    ram_mb: int

    @property
    def template_name(self) -> str:
        return f"mcpolis-{self.language}-cpu{self.cpu}-ram{self.ram_mb}"

    @property
    def dockerfile_path(self) -> Path:
        return TEMPLATES_DIR / f"Dockerfile.{self.language}"


def grid() -> list[Combo]:
    """Materialize the 24-combo matrix."""
    return [
        Combo(language=lang, cpu=cpu, ram_mb=ram)
        for lang in LANGUAGES
        for cpu, ram in CPU_RAM_PAIRS
    ]


async def build_one(
    combo: Combo, *, api_key: str, skip_cache: bool,
) -> str:
    """Build (or rebuild) one published template via the v2 SDK.

    Returns the template name on success; raises whatever the SDK
    raises on failure. Caller decides whether one combo's failure
    aborts the whole grid or just gets logged.
    """
    # Lazy import so ``--check`` / ``--list-only`` can run without
    # the e2b SDK installed (matches RealE2BClient's pattern). The
    # ``AsyncTemplate`` class is re-exported at the package root;
    # the inner ``e2b.template_async`` submodule does NOT re-export
    # it (only ``main`` and ``build_api`` are surfaced there), so
    # the public ``e2b.AsyncTemplate`` is the right import path.
    from e2b import AsyncTemplate  # noqa: PLC0415

    if not combo.dockerfile_path.exists():
        raise FileNotFoundError(
            f"Dockerfile not found: {combo.dockerfile_path}",
        )
    dockerfile_content = combo.dockerfile_path.read_text()

    # E2B's v2 template parser rejects Dockerfile comment lines
    # ("Unsupported instruction: COMMENT") when ``set_start_cmd`` is
    # used — the stricter parsing path that ``set_start_cmd`` triggers
    # doesn't handle ``#`` lines. Strip them before passing content to
    # ``from_dockerfile``. Blank lines are fine and kept for legibility.
    dockerfile_stripped = "\n".join(
        line for line in dockerfile_content.splitlines()
        if not line.lstrip().startswith("#")
    )

    # ``from_dockerfile`` accepts either a path or raw content.
    # Passing content keeps the API call self-contained — no
    # working-directory assumption.
    template_builder = AsyncTemplate().from_dockerfile(dockerfile_stripped)

    # node / python have no start command — E2B spawns the MCP command
    # directly. docker templates also have no set_start_cmd here:
    # dockerd is started by E2BSandboxService._start_docker_daemon()
    # after the sandbox boots, because E2B validates set_start_cmd during
    # build in an environment that already has a Docker daemon running,
    # which causes "process with PID N is still running" and fails the
    # build. The adapter-layer approach avoids the build-time conflict
    # entirely.

    print(
        f"==> building {combo.template_name} "
        f"(cpu={combo.cpu}, ram={combo.ram_mb}MiB)",
        flush=True,
    )

    def _on_log(entry: object) -> None:
        # The SDK pipes E2B build output through here; surface it
        # to stdout so the operator can watch progress.
        message = getattr(entry, "message", None) or str(entry)
        # Prefix per-combo so concurrent operator eyeballs aren't
        # confused if a future driver builds combos in parallel.
        print(f"  [{combo.template_name}] {message}", flush=True)

    await AsyncTemplate.build(
        template=template_builder,
        name=combo.template_name,
        cpu_count=combo.cpu,
        memory_mb=combo.ram_mb,
        skip_cache=skip_cache,
        on_build_logs=_on_log,
        api_key=api_key,
    )
    print(f"<== built {combo.template_name}", flush=True)
    return combo.template_name


async def build_all(
    *, api_key: str, skip_cache: bool, fail_fast: bool,
) -> tuple[list[str], list[tuple[Combo, BaseException]]]:
    """Build every combo in the grid sequentially.

    Sequential rather than concurrent because:
    - E2B's per-account build concurrency limit is low (2-3 at peak),
      so parallel builds get throttled or rejected anyway.
    - Sequential output is much easier to read in an operator log.
    - The whole 24-combo grid finishes in ~8-12 minutes; not worth
      the extra error-handling complexity.

    Returns ``(succeeded, failed)`` where ``failed`` carries the
    exception per combo for an end-of-run summary.
    """
    succeeded: list[str] = []
    failed: list[tuple[Combo, BaseException]] = []
    for combo in grid():
        try:
            await build_one(
                combo, api_key=api_key, skip_cache=skip_cache,
            )
            succeeded.append(combo.template_name)
        except BaseException as exc:  # noqa: BLE001
            failed.append((combo, exc))
            print(
                f"!! build failed for {combo.template_name}: "
                f"{exc.__class__.__name__}: {exc}",
                flush=True,
            )
            if fail_fast:
                break
    return succeeded, failed


def _check_consistency() -> int:
    """Validate the v2 grid matches the v1 generator's matrix and
    that every Dockerfile exists. Used by CI lint.
    """
    combos = grid()
    if len(combos) != 24:
        print(f"FAIL: expected 24 combos, got {len(combos)}", file=sys.stderr)
        return 1
    names = [c.template_name for c in combos]
    if len(set(names)) != len(names):
        print("FAIL: duplicate template names in matrix", file=sys.stderr)
        return 1
    for combo in combos:
        if not combo.dockerfile_path.exists():
            print(
                f"FAIL: missing dockerfile for {combo.template_name}: "
                f"{combo.dockerfile_path}",
                file=sys.stderr,
            )
            return 1
    print(f"OK: v2 grid carries {len(combos)} combos with valid Dockerfiles")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Validate the matrix + Dockerfile presence without "
            "talking to E2B. Used by CI."
        ),
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print the 24 template names and exit; no API calls.",
    )
    parser.add_argument(
        "--skip-cache",
        action="store_true",
        help="Force a fresh build (ignore E2B's build cache).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help=(
            "Stop on the first combo failure instead of continuing "
            "and reporting at the end."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "E2B API key. Defaults to MCPOLIS_E2B_API_KEY then "
            "E2B_API_KEY environment variables."
        ),
    )
    args = parser.parse_args()

    if args.check:
        return _check_consistency()

    if args.list_only:
        for combo in grid():
            print(combo.template_name)
        return 0

    api_key = (
        args.api_key
        or os.environ.get("MCPOLIS_E2B_API_KEY")
        or os.environ.get("E2B_API_KEY")
    )
    if not api_key:
        print(
            "ERROR: no API key. Set MCPOLIS_E2B_API_KEY or "
            "E2B_API_KEY in the environment, or pass --api-key.",
            file=sys.stderr,
        )
        return 1

    succeeded, failed = asyncio.run(
        build_all(
            api_key=api_key,
            skip_cache=args.skip_cache,
            fail_fast=args.fail_fast,
        ),
    )
    print()
    print(f"--- summary: {len(succeeded)} succeeded, {len(failed)} failed ---")
    for name in succeeded:
        print(f"  OK  {name}")
    for combo, exc in failed:
        print(f"  FAIL {combo.template_name}: {exc.__class__.__name__}: {exc}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
