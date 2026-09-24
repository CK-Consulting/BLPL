"""Emit ``sym-lib-table`` and ``fp-lib-table`` beside the compiled project.

Without these, KiCad has no idea where ``Device:R`` or ``Capacitor_SMD:C_0402``
live. It does not fail — it loads the design, resolves nothing, and ERC reports
"The current configuration does not include the symbol library 'Device'" once
per reference. On the example-handheld project that was **533 of 762 violations
across seven boards, 70% of the total**, which buried the real findings under
library bookkeeping and made every board's ERC unreadable.

The tables list only the libraries the design actually references, resolved
against the same ordered roots the symbol and footprint emitters use, so a
project-local library shadows a stock one here exactly as it does there.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import sexpr


def _roots(root: Path | str | Sequence[Path | str]) -> list[Path]:
    if isinstance(root, (str, Path)):
        return [Path(root)]
    return [Path(r) for r in root]


def _nicknames(hdm: dict, key: str, extra: Sequence[str] = ()) -> list[str]:
    """Distinct library nicknames referenced by the design, in sorted order."""
    comps = hdm.get("components") or {}
    values = comps.values() if isinstance(comps, dict) else comps
    names = set()
    for comp in values:
        ref = (comp or {}).get(key) or ""
        if ":" in ref:
            names.add(ref.split(":", 1)[0])
    for ref in extra:
        if ":" in ref:
            names.add(ref.split(":", 1)[0])
    return sorted(names)


def _find_symbol_lib(lib: str, roots: list[Path]) -> Path | None:
    """First root holding this symbol library, flat file or v10 per-symbol dir."""
    for root in roots:
        flat = root / f"{lib}.kicad_sym"
        if flat.is_file():
            return flat
        symdir = root / f"{lib}.kicad_symdir"
        if symdir.is_dir():
            return symdir
    return None


def _find_footprint_lib(lib: str, roots: list[Path]) -> Path | None:
    for root in roots:
        pretty = root / f"{lib}.pretty"
        if pretty.is_dir():
            return pretty
    return None


def _table(kind: str, entries: list[tuple[str, Path]]) -> str:
    lines = [f"({kind}", "  (version 7)"]
    for name, uri in entries:
        lines.append(
            f"  (lib (name {sexpr.quote(name)})(type \"KiCad\")"
            f"(uri {sexpr.quote(str(uri))})(options \"\")(descr \"\"))"
        )
    lines.append(")")
    return "\n".join(lines) + "\n"


def write(
    hdm: dict,
    output_dir: Path,
    *,
    symbols_root: Path | str | Sequence[Path | str],
    footprints_root: Path | str | Sequence[Path | str],
    extra_symbol_lib_ids: Sequence[str] = (),
) -> dict[str, list[str]]:
    """Write both tables into ``output_dir``.

    Returns ``{"symbols": [...], "footprints": [...], "unresolved": [...]}`` —
    the libraries written, and any nickname the design references that no root
    provides. An unresolved nickname is not fatal here: the emitters have
    already placed the reference, and leaving it out of the table reproduces
    exactly the old behaviour for that one library rather than for all of them.
    """
    output_dir = Path(output_dir)
    sym_roots = _roots(symbols_root)
    fp_roots = _roots(footprints_root)

    sym_entries: list[tuple[str, Path]] = []
    fp_entries: list[tuple[str, Path]] = []
    unresolved: list[str] = []

    for lib in _nicknames(hdm, "lib_symbol", extra_symbol_lib_ids):
        found = _find_symbol_lib(lib, sym_roots)
        if found is None:
            unresolved.append(f"symbol:{lib}")
        else:
            sym_entries.append((lib, found.resolve()))

    for lib in _nicknames(hdm, "footprint"):
        found = _find_footprint_lib(lib, fp_roots)
        if found is None:
            unresolved.append(f"footprint:{lib}")
        else:
            fp_entries.append((lib, found.resolve()))

    (output_dir / "sym-lib-table").write_text(_table("sym_lib_table", sym_entries), encoding="utf-8")
    (output_dir / "fp-lib-table").write_text(_table("fp_lib_table", fp_entries), encoding="utf-8")

    return {
        "symbols": [n for n, _ in sym_entries],
        "footprints": [n for n, _ in fp_entries],
        "unresolved": unresolved,
    }
