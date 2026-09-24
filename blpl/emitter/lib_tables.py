"""Emit ``sym-lib-table`` and ``fp-lib-table`` beside the compiled project.

Without these, KiCad has no idea where ``Device:R`` or ``Capacitor_SMD:C_0402``
live. It does not fail — it loads the design, resolves nothing, and ERC reports
"The current configuration does not include the symbol library 'Device'" once
per reference. On the example-handheld project that was **533 of 762 violations
across seven boards, 70% of the total**, which buried the real findings under
library bookkeeping.

**The hard part is not listing the libraries, it is that a table cannot say
what the loaders can.** ``loaders`` resolves *per item*: it walks the ordered
roots looking for one footprint, so it will happily take
``Package_LGA:LGA-8_8x6mm_P1.27mm`` from stock even though
``Package_LGA:Nordic_LGA-113`` came from a vendored module library of the same
nickname. A lib table maps a nickname to **exactly one** directory, so it
cannot express that, and naively taking the first root holding
``<Lib>.pretty`` points KiCad at a library missing most of what the design
uses — which is how ``LGA-8_8x6mm_P1.27mm not found in library 'Package_LGA'``
appeared on a board whose stock library contains it.

So each nickname is resolved against the *items the design actually
references*. A root that serves all of them is used directly. When no single
root does, the referenced items are copied into a merged library under the
output directory and the table points there, which is the only arrangement
that makes the table agree with what the emitters loaded.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from . import sexpr

MERGE_DIRNAME = "lib-merge"


def _roots(root: Path | str | Sequence[Path | str]) -> list[Path]:
    if isinstance(root, (str, Path)):
        return [Path(root)]
    return [Path(r) for r in root]


def _referenced(hdm: dict, key: str, extra: Sequence[str] = ()) -> dict[str, set[str]]:
    """Nickname → the item names the design references from it."""
    comps = hdm.get("components") or {}
    values = comps.values() if isinstance(comps, dict) else comps
    out: dict[str, set[str]] = defaultdict(set)
    for ref in [(c or {}).get(key) or "" for c in values] + list(extra):
        if ":" in ref:
            lib, name = ref.split(":", 1)
            out[lib].add(name)
    return dict(out)


def _symbol_holder(root: Path, lib: str, name: str) -> Path | None:
    """Where this one symbol lives under this root, mirroring ``loaders``."""
    symdir = root / f"{lib}.kicad_symdir" / f"{name}.kicad_sym"
    if symdir.is_file():
        return root / f"{lib}.kicad_symdir"
    flat = root / f"{lib}.kicad_sym"
    if flat.is_file():
        try:
            if sexpr.quote(name) in flat.read_text(encoding="utf-8", errors="replace"):
                return flat
        except OSError:
            return None
    return None


def _footprint_holder(root: Path, lib: str, name: str) -> Path | None:
    pretty = root / f"{lib}.pretty"
    return pretty if (pretty / f"{name}.kicad_mod").is_file() else None


def _resolve(lib: str, items: set[str], roots: list[Path], holder) -> tuple[Path | None, dict[str, Path]]:
    """First root serving every referenced item, plus each item's own source.

    The per-item map is what a merge needs, and it uses the same root order the
    loaders use, so a merged library contains exactly the files the emitters read.
    """
    per_item: dict[str, Path] = {}
    for name in sorted(items):
        for root in roots:
            found = holder(root, lib, name)
            if found is not None:
                per_item[name] = found
                break
    for root in roots:
        if all(holder(root, lib, name) is not None for name in items):
            return root, per_item
    return None, per_item


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
) -> dict:
    """Write both tables into ``output_dir``.

    Returns the libraries written, any nickname split across roots (and so
    merged, or reported), and any referenced item no root provides.
    """
    output_dir = Path(output_dir)
    sym_roots = _roots(symbols_root)
    fp_roots = _roots(footprints_root)
    report: dict = {"symbols": [], "footprints": [], "merged": [], "unresolved": []}

    sym_entries: list[tuple[str, Path]] = []
    for lib, items in sorted(_referenced(hdm, "lib_symbol", extra_symbol_lib_ids).items()):
        root, per_item = _resolve(lib, items, sym_roots, _symbol_holder)
        missing = sorted(items - set(per_item))
        for name in missing:
            report["unresolved"].append(f"symbol:{lib}:{name}")
        if root is not None:
            held = _symbol_holder(root, lib, sorted(items)[0])
            sym_entries.append((lib, (held or root).resolve()))
            report["symbols"].append(lib)
        elif per_item:
            # No merge for symbols: a flat .kicad_sym would have to be parsed and
            # re-emitted, and nothing in the example-handheld design needs it. Reported
            # rather than guessed, because silently picking one root here is the
            # bug this module exists to fix.
            report["merged"].append({"kind": "symbol", "lib": lib, "merged": False,
                                     "sources": sorted({str(p) for p in per_item.values()})})

    fp_entries: list[tuple[str, Path]] = []
    for lib, items in sorted(_referenced(hdm, "footprint").items()):
        root, per_item = _resolve(lib, items, fp_roots, _footprint_holder)
        for name in sorted(items - set(per_item)):
            report["unresolved"].append(f"footprint:{lib}:{name}")
        if root is not None:
            fp_entries.append((lib, (root / f"{lib}.pretty").resolve()))
            report["footprints"].append(lib)
        elif per_item:
            merged = output_dir / MERGE_DIRNAME / f"{lib}.pretty"
            merged.mkdir(parents=True, exist_ok=True)
            for name, src in per_item.items():
                shutil.copyfile(src / f"{name}.kicad_mod", merged / f"{name}.kicad_mod")
            fp_entries.append((lib, merged.resolve()))
            report["footprints"].append(lib)
            report["merged"].append({"kind": "footprint", "lib": lib, "merged": True,
                                     "items": sorted(per_item),
                                     "sources": sorted({str(p) for p in per_item.values()})})

    (output_dir / "sym-lib-table").write_text(_table("sym_lib_table", sym_entries), encoding="utf-8")
    (output_dir / "fp-lib-table").write_text(_table("fp_lib_table", fp_entries), encoding="utf-8")
    return report
