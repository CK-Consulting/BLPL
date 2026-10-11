"""Guard the library root paths.

These were computed with `Path(__file__).resolve().parent.parent`, which lands on
`blpl/` rather than the repo root — so every symbol and footprint lookup missed,
and every KLC check reported "tool not found, skipped".

Nothing failed loudly. Stage 6 caught LibraryMiss and carried on, so it emitted
schematics whose symbol instances referenced lib_ids with no lib_symbols entry
(unopenable in KiCad and hangs browser viewers) and PCBs with no footprints. Stage 7
reported ok=true while never running.

A wrong path that degrades silently is the worst kind, so assert the roots resolve.
"""

from __future__ import annotations

from blpl.core import stage6_compile_kicad as s6
from blpl.core import stage7_validate as s7


def test_symbol_library_root_exists() -> None:
    assert s6._DEFAULT_SYMBOLS.exists(), (
        f"symbol library root does not exist: {s6._DEFAULT_SYMBOLS}. "
        "Every symbol lookup will miss and the emitter will write dangling lib_ids."
    )


def test_footprint_library_root_exists() -> None:
    assert s6._DEFAULT_FOOTPRINTS.exists(), (
        f"footprint library root does not exist: {s6._DEFAULT_FOOTPRINTS}. "
        "Every footprint lookup will miss and the PCB will be emitted empty."
    )


def test_symbol_root_holds_real_kicad_libraries() -> None:
    """Existing isn't enough — an empty submodule checkout also 'exists'."""
    libs = list(s6._DEFAULT_SYMBOLS.glob("*.kicad_symdir"))
    assert libs, f"no *.kicad_symdir libraries under {s6._DEFAULT_SYMBOLS}"


def test_klc_checker_is_actually_reachable() -> None:
    """Stage 7's KLC check degrades to `skipped` when this is missing — so a
    wrong path shows up as a passing validation run, not a failing one."""
    assert s7._CHECK_SYMBOL_PY.exists(), (
        f"KLC checker not found at {s7._CHECK_SYMBOL_PY}; stage7 will silently "
        "report KLC as skipped while still returning ok."
    )
