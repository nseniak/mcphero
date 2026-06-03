"""E2B template grid — Python source of truth for the published
``runner/e2b-templates/`` matrix.

The operator-side generator at ``runner/e2b-templates/generate_templates.py``
emits one ``e2b.toml`` per entry here; this module re-encodes the
same matrix so :class:`E2BSandboxService.capabilities` and
``validate_resources`` stay in lockstep without forcing a runtime
import of the generator script.

A test in ``tests/test_e2b_template_grid_consistency.py`` enforces
the two sources never drift.
"""
from __future__ import annotations

from typing import Literal

E2BLanguage = Literal["node", "python", "docker"]

# (cpu_vcpus, ram_mb) pairings, mirrored from
# ``runner/e2b-templates/generate_templates.py``.
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

LANGUAGES: tuple[E2BLanguage, ...] = ("node", "python", "docker")


class E2BTemplateGrid:
    """Lookup table: ``(language, cpu_vcpus, ram_mb) → template_name``.

    Constructed once per ``E2BSandboxService`` instance from the
    module-level ``CPU_RAM_PAIRS`` + ``LANGUAGES``. Pre-computes the
    valid-pairings set so ``validate_resources`` short-circuits in
    O(1).
    """

    def __init__(
        self,
        languages: tuple[E2BLanguage, ...] = LANGUAGES,
        cpu_ram_pairs: tuple[tuple[int, int], ...] = CPU_RAM_PAIRS,
    ) -> None:
        self._languages: tuple[E2BLanguage, ...] = languages
        self._cpu_ram_pairs: tuple[tuple[int, int], ...] = cpu_ram_pairs
        self._allowed_cpus: tuple[float, ...] = tuple(
            sorted({float(cpu) for cpu, _ in cpu_ram_pairs}),
        )
        self._allowed_rams: tuple[int, ...] = tuple(
            sorted({ram for _, ram in cpu_ram_pairs}),
        )
        self._pair_set: set[tuple[int, int]] = set(cpu_ram_pairs)

    @property
    def allowed_cpu_vcpus(self) -> tuple[float, ...]:
        return self._allowed_cpus

    @property
    def allowed_memory_mb(self) -> tuple[int, ...]:
        return self._allowed_rams

    @property
    def cpu_ram_pairs(self) -> tuple[tuple[int, int], ...]:
        return self._cpu_ram_pairs

    def is_valid_pairing(self, *, cpu_vcpus: float, memory_mb: int) -> bool:
        """Return whether the (cpu, ram) tuple is published.

        Float CPUs that aren't whole integers are rejected outright
        (the grid is integer-CPU only — the SDK limits ``cpu_count``
        to integer values).
        """
        if cpu_vcpus != int(cpu_vcpus):
            return False
        return (int(cpu_vcpus), memory_mb) in self._pair_set

    def template_name(
        self, *, language: E2BLanguage, cpu_vcpus: float, memory_mb: int,
    ) -> str:
        """Return the published template name for the requested combo.

        Raises ``KeyError`` if the combo isn't published (the caller
        should have called ``is_valid_pairing`` first; the raise is
        defense-in-depth).
        """
        if not self.is_valid_pairing(cpu_vcpus=cpu_vcpus, memory_mb=memory_mb):
            raise KeyError(
                f"({cpu_vcpus}, {memory_mb}) not in E2B template grid",
            )
        if language not in self._languages:
            raise KeyError(f"unsupported E2B language {language!r}")
        return f"mcpolis-{language}-cpu{int(cpu_vcpus)}-ram{memory_mb}"


def template_name_for(
    *,
    language: E2BLanguage,
    cpu_vcpus: float,
    memory_mb: int,
) -> str:
    """Module-level shortcut. Useful for callers that don't hold a
    grid instance."""
    return E2BTemplateGrid().template_name(
        language=language, cpu_vcpus=cpu_vcpus, memory_mb=memory_mb,
    )


def language_for_command(command: str) -> E2BLanguage | None:
    """Map a stdio command (npx / uvx / etc.) to a language tag.

    Returns ``None`` for commands the grid doesn't cover; the caller
    fails fast with ``ResourcesUnsupported`` rather than picking a
    surprising fallback.
    """
    cmd = command.strip().lower()
    if cmd in {"npx", "node"}:
        return "node"
    if cmd in {"uvx", "uv", "python", "python3"}:
        return "python"
    if cmd == "docker":
        # Docker-distributed MCPs run as ``docker run -i --rm <image>``
        # inside a dind-capable sandbox whose ``dockerd`` is started at
        # boot via the template's ``set_start_cmd``. ``docker compose``
        # is a subcommand of the same binary, so it's covered too.
        return "docker"
    return None


__all__ = [
    "CPU_RAM_PAIRS",
    "E2BLanguage",
    "E2BTemplateGrid",
    "LANGUAGES",
    "language_for_command",
    "template_name_for",
]
