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

**One directory, seven boards, one filename.** KiCad finds a library table by
looking for a file named exactly ``sym-lib-table`` beside the ``.kicad_pro``,
so the canonical pair cannot be board-qualified without KiCad ceasing to find
them at all. But every board of a multi-board project compiles into the same
``.pipeline/``, so each board's table used to overwrite the last one's, and
opening any board except the most recently compiled gave you another board's
libraries. ERC never caught it because ERC runs immediately after the stage 6
that wrote the table — it is the person opening an older board in KiCad who
pays.

So both are written. ``sym-lib-table.<board>`` is the per-board record, which
is what anything reading a specific board's libraries should use. The
canonical ``sym-lib-table`` is the **union** of every board's, so whichever
board KiCad opens resolves correctly from the one file it is willing to read.
A nickname two boards resolve to *different* roots cannot be expressed in one
table at all; that is reported rather than silently decided, and the
currently-compiling board wins the entry.
"""

from __future__ import annotations

import json
import re
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


ITEMS_STEM = "lib-items"


def _read_items(output_dir: Path, board: str | None) -> dict[str, dict[str, dict[str, str]]]:
    """Every board's ``{kind: {lib: {item: root}}}``, this one's last.

    A table maps a nickname to one directory. Whether several boards can share
    one entry is not a question about nicknames, it is a question about the
    *items* behind them — which is the same realisation that made per-item
    resolution necessary within a single board, applied across boards.
    """
    out: dict[str, dict[str, dict[str, str]]] = {}
    for sidecar in sorted(output_dir.glob(f"{ITEMS_STEM}.*.json")):
        if board is not None and sidecar.name == f"{ITEMS_STEM}.{board}.json":
            continue  # rewritten below; do not read the previous run's copy
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for kind, libs in data.items():
            for lib, items in libs.items():
                out.setdefault(kind, {}).setdefault(lib, {}).update(items)
    return out


def _union_entries(
    kind: str,
    all_items: dict[str, dict[str, str]],
    output_dir: Path,
    mergeable: bool,
    current: dict[str, str],
) -> tuple[list[tuple[str, Path]], list[dict]]:
    """One entry per nickname, across every board compiled into this directory.

    When all of a nickname's items — from every board — live under one root,
    that root is the entry. When they do not, the items are copied into a
    merged library and the entry points there, exactly as the single-board path
    does. Footprints can be merged that way; symbols cannot, so a symbol
    disagreement is reported and the compiling board's answer is used.
    """
    entries: list[tuple[str, Path]] = []
    conflicts: list[dict] = []
    for lib, items in sorted(all_items.items()):
        roots = sorted(set(items.values()))
        if len(roots) == 1:
            entries.append((lib, Path(roots[0])))
            continue
        if not mergeable:
            chosen = current.get(lib, roots[0])
            conflicts.append({"kind": kind, "lib": lib, "merged": False,
                              "chosen": chosen, "candidates": roots})
            entries.append((lib, Path(chosen)))
            continue
        merged = output_dir / MERGE_DIRNAME / f"{lib}.pretty"
        merged.mkdir(parents=True, exist_ok=True)
        copied = []
        for name, root in sorted(items.items()):
            src = Path(root) / f"{name}.kicad_mod"
            if src.is_file():
                shutil.copyfile(src, merged / f"{name}.kicad_mod")
                copied.append(name)
        entries.append((lib, merged.resolve()))
        conflicts.append({"kind": kind, "lib": lib, "merged": True,
                          "items": copied, "candidates": roots,
                          "chosen": str(merged.resolve())})
    return entries, conflicts


def write(
    hdm: dict,
    output_dir: Path,
    *,
    symbols_root: Path | str | Sequence[Path | str],
    footprints_root: Path | str | Sequence[Path | str],
    extra_symbol_lib_ids: Sequence[str] = (),
    board: str | None = None,
) -> dict:
    """Write both tables into ``output_dir``.

    ``board``, when given, also writes ``sym-lib-table.<board>`` and
    ``fp-lib-table.<board>`` and makes the canonical pair a union across every
    board compiled into this directory. See the module docstring for why the
    canonical names cannot themselves be qualified.

    Returns the libraries written, any nickname split across roots (and so
    merged, or reported), any referenced item no root provides, and any
    nickname two boards disagree about.
    """
    output_dir = Path(output_dir)
    sym_roots = _roots(symbols_root)
    fp_roots = _roots(footprints_root)
    report: dict = {"symbols": [], "footprints": [], "merged": [], "unresolved": [], "conflicts": []}

    sym_entries: list[tuple[str, Path]] = []
    sym_items: dict[str, dict[str, str]] = {}
    for lib, items in sorted(_referenced(hdm, "lib_symbol", extra_symbol_lib_ids).items()):
        root, per_item = _resolve(lib, items, sym_roots, _symbol_holder)
        missing = sorted(items - set(per_item))
        for name in missing:
            report["unresolved"].append(f"symbol:{lib}:{name}")
        if root is not None:
            held = _symbol_holder(root, lib, sorted(items)[0])
            sym_entries.append((lib, (held or root).resolve()))
            sym_items[lib] = {n: str((held or root).resolve()) for n in sorted(items)}
            report["symbols"].append(lib)
        elif per_item:
            # No merge for symbols: a flat .kicad_sym would have to be parsed and
            # re-emitted, and nothing in the example-handheld design needs it. Reported
            # rather than guessed, because silently picking one root here is the
            # bug this module exists to fix.
            report["merged"].append({"kind": "symbol", "lib": lib, "merged": False,
                                     "sources": sorted({str(p) for p in per_item.values()})})

    fp_entries: list[tuple[str, Path]] = []
    fp_items: dict[str, dict[str, str]] = {}
    for lib, items in sorted(_referenced(hdm, "footprint").items()):
        root, per_item = _resolve(lib, items, fp_roots, _footprint_holder)
        for name in sorted(items - set(per_item)):
            report["unresolved"].append(f"footprint:{lib}:{name}")
        if root is not None:
            fp_entries.append((lib, (root / f"{lib}.pretty").resolve()))
            fp_items[lib] = {n: str((root / f"{lib}.pretty").resolve()) for n in sorted(items)}
            report["footprints"].append(lib)
        elif per_item:
            merged = output_dir / MERGE_DIRNAME / f"{lib}.pretty"
            merged.mkdir(parents=True, exist_ok=True)
            for name, src in per_item.items():
                shutil.copyfile(src / f"{name}.kicad_mod", merged / f"{name}.kicad_mod")
            fp_entries.append((lib, merged.resolve()))
            fp_items[lib] = {n: str((src / "").resolve()) for n, src in per_item.items()}
            report["footprints"].append(lib)
            report["merged"].append({"kind": "footprint", "lib": lib, "merged": True,
                                     "items": sorted(per_item),
                                     "sources": sorted({str(p) for p in per_item.values()})})

    if board is None:
        # Single-board project: nothing to collide with, and no sidecar to keep.
        (output_dir / "sym-lib-table").write_text(_table("sym_lib_table", sym_entries), encoding="utf-8")
        (output_dir / "fp-lib-table").write_text(_table("fp_lib_table", fp_entries), encoding="utf-8")
        return report

    mine = {"symbols": sym_items, "footprints": fp_items}
    (output_dir / f"{ITEMS_STEM}.{board}.json").write_text(
        json.dumps(mine, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    others = _read_items(output_dir, board)
    for stem, kind, entries, key, mergeable in (
        ("sym-lib-table", "sym_lib_table", sym_entries, "symbols", False),
        ("fp-lib-table", "fp_lib_table", fp_entries, "footprints", True),
    ):
        (output_dir / f"{stem}.{board}").write_text(_table(kind, entries), encoding="utf-8")
        combined: dict[str, dict[str, str]] = {}
        for lib, items in others.get(key, {}).items():
            combined.setdefault(lib, {}).update(items)
        # Two boards wanting the *same* item from different roots is a real
        # disagreement that no single table can hold, and merging cannot help
        # either: one file has to win. Record it before this board's answer
        # overwrites the other's, or the collapse is silent.
        item_clashes: list[dict] = []
        for lib, items in mine[key].items():
            have = combined.setdefault(lib, {})
            for name, root in items.items():
                if name in have and have[name] != root:
                    item_clashes.append({
                        "kind": key, "lib": lib, "item": name, "merged": False,
                        "chosen": root, "candidates": sorted({have[name], root}),
                    })
                have[name] = root  # this board wins its own items
        union, conflicts = _union_entries(
            key, combined, output_dir, mergeable, {n: str(u) for n, u in entries}
        )
        conflicts = item_clashes + conflicts
        (output_dir / stem).write_text(_table(kind, union), encoding="utf-8")
        report["conflicts"].extend(conflicts)
    return report
