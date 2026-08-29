"""Stage 2: library lookup.

Reads a BOM (bom.json, validated against schemas/bom.v1.json) and scans the
configured KiCad symbol and footprint libraries to produce a coverage report
(schemas/coverage_report.v1.json).

Deterministic. No LLM. No I/O beyond filesystem reads.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import schema


_SYMBOL_LIB_SUFFIX = ".kicad_symdir"
_SYMBOL_FILE_SUFFIX = ".kicad_sym"
_FOOTPRINT_LIB_SUFFIX = ".pretty"
_FOOTPRINT_FILE_SUFFIX = ".kicad_mod"


@dataclass(frozen=True)
class _LibEntry:
    lib: str
    name: str
    path: Path


def _index_symbols(root: Path) -> dict[str, list[_LibEntry]]:
    """Scan `root` for `*.kicad_symdir/*.kicad_sym` and build a name->entries index.

    The symbol *name* is the filename stem. We return a dict keyed by the
    normalized name so fuzzy matches can be attempted against normalized tokens.
    """
    index: dict[str, list[_LibEntry]] = {}
    if not root.exists():
        return index
    for lib_dir in sorted(root.iterdir()):
        if not lib_dir.is_dir() or not lib_dir.name.endswith(_SYMBOL_LIB_SUFFIX):
            continue
        lib_name = lib_dir.name[: -len(_SYMBOL_LIB_SUFFIX)]
        for sym_file in sorted(lib_dir.glob(f"*{_SYMBOL_FILE_SUFFIX}")):
            sym_name = sym_file.stem
            entry = _LibEntry(lib=lib_name, name=sym_name, path=sym_file)
            index.setdefault(_normalize(sym_name), []).append(entry)
    # Flat <Lib>.kicad_sym files — one library, many symbols — are the layout
    # Stage 3 GENERATES, and symbol_resolution has always resolved them. The
    # index only ever read the directory layout, so every generated symbol was
    # invisible to coverage and got re-reported as a gap forever.
    for flat in sorted(root.glob(f"*{_SYMBOL_FILE_SUFFIX}")):
        lib_name = flat.stem
        try:
            text = flat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'\(symbol "([^":]+)"', text):
            entry = _LibEntry(lib=lib_name, name=m.group(1), path=flat)
            index.setdefault(_normalize(m.group(1)), []).append(entry)
    return index


def _index_footprints(root: Path) -> dict[str, list[_LibEntry]]:
    """Scan `root` for `*.pretty/*.kicad_mod` and build a name->entries index."""
    index: dict[str, list[_LibEntry]] = {}
    if not root.exists():
        return index
    for lib_dir in sorted(root.iterdir()):
        if not lib_dir.is_dir() or not lib_dir.name.endswith(_FOOTPRINT_LIB_SUFFIX):
            continue
        lib_name = lib_dir.name[: -len(_FOOTPRINT_LIB_SUFFIX)]
        for fp_file in sorted(lib_dir.glob(f"*{_FOOTPRINT_FILE_SUFFIX}")):
            fp_name = fp_file.stem
            entry = _LibEntry(lib=lib_name, name=fp_name, path=fp_file)
            index.setdefault(_normalize(fp_name), []).append(entry)
    return index


_NORMALIZE_SPLIT = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    return _NORMALIZE_SPLIT.sub("_", s.lower()).strip("_")


def _tokenize(s: str) -> set[str]:
    return {t for t in _normalize(s).split("_") if t}


def _lookup_exact(hint: str | None, all_entries: dict[str, list[_LibEntry]]) -> _LibEntry | None:
    """Match a 'Lib:Name' hint directly against the index.

    Uses normalized name matching (not the raw key) because the hint's `Name` part
    may differ from canonical naming by punctuation only.
    """
    if not hint or ":" not in hint:
        return None
    lib_hint, name_hint = hint.split(":", 1)
    norm_name = _normalize(name_hint)
    candidates = all_entries.get(norm_name, [])
    for entry in candidates:
        if entry.lib == lib_hint:
            return entry
    return None


def _lookup_fuzzy(
    query_tokens: set[str],
    all_entries: dict[str, list[_LibEntry]],
    lib_filter: str | None = None,
) -> tuple[_LibEntry, float] | None:
    """Find the best fuzzy match by Jaccard similarity over normalized tokens.

    Returns the (entry, score) with the highest score >= 0.5, or None.
    If lib_filter is provided, restricts candidates to that library.
    """
    if not query_tokens:
        return None
    best: tuple[_LibEntry, float] | None = None
    for norm_name, entries in all_entries.items():
        cand_tokens = {t for t in norm_name.split("_") if t}
        if not cand_tokens:
            continue
        overlap = query_tokens & cand_tokens
        if not overlap:
            continue
        union = query_tokens | cand_tokens
        score = len(overlap) / len(union)
        if score < 0.5:
            continue
        for entry in entries:
            if lib_filter is not None and entry.lib != lib_filter:
                continue
            if best is None or score > best[1]:
                best = (entry, score)
    return best


def _symbol_query_tokens(row: dict) -> set[str]:
    """Tokens we search symbol names against: MPN + part family hints."""
    tokens: set[str] = set()
    for key in ("mpn", "part_hint", "symbol_hint"):
        v = row.get(key)
        if isinstance(v, str):
            if ":" in v and key == "symbol_hint":
                v = v.split(":", 1)[1]
            tokens |= _tokenize(v)
    return tokens


def _footprint_query_tokens(row: dict) -> set[str]:
    tokens: set[str] = set()
    for key in ("package", "footprint_hint"):
        v = row.get(key)
        if isinstance(v, str):
            if ":" in v and key == "footprint_hint":
                v = v.split(":", 1)[1]
            tokens |= _tokenize(v)
    return tokens


def _lookup_mpn(mpn: str | None, all_entries: dict[str, list[_LibEntry]]) -> tuple[_LibEntry, float] | None:
    """Match an MPN against library names by containment, not token overlap.

    Token-set Jaccard cannot see that BGS12P2L6 names the same silicon as the
    library's BGS12P2L6E6327XTSA1 — one token each, zero overlap — nor that
    PAM8302AASCR is the stock PAM8302AAS plus a packing suffix. Flattened
    containment can, and it is the same semantic the component library's
    fuzzy lookup already uses. Six characters minimum on the contained side,
    because every part a manufacturer makes shares its first few.
    """
    if not mpn:
        return None
    flat_m = re.sub(r"[^a-z0-9]", "", mpn.lower())
    if len(flat_m) < 6:
        return None
    best: tuple[_LibEntry, float] | None = None
    for key, entries in all_entries.items():
        flat_k = key.replace("_", "")
        if flat_m in flat_k or (len(flat_k) >= 6 and flat_k in flat_m):
            score = min(len(flat_k), len(flat_m)) / max(len(flat_k), len(flat_m))
            if best is None or score > best[1]:
                best = (entries[0], score)
    return best


def _entry_to_match(entry: _LibEntry, match_type: str, score: float | None, root: Path) -> dict:
    m: dict = {
        "lib": entry.lib,
        "name": entry.name,
        "path": str(entry.path.relative_to(root.parent)) if root in entry.path.parents else str(entry.path),
        "match_type": match_type,
    }
    if score is not None:
        m["score"] = round(score, 3)
    return m


def _merged_index(roots, index_one) -> dict[str, list[_LibEntry]]:
    """One index over several roots, earlier roots listed first per name.

    Order matters because _lookup_exact and _lookup_fuzzy take the first
    acceptable candidate: a part the project vendored or the shared library
    holds must beat a stock part of the same name, exactly as it does at
    resolution time in Stage 5.
    """
    merged: dict[str, list[_LibEntry]] = {}
    for root in roots:
        for key, entries in index_one(Path(root)).items():
            merged.setdefault(key, []).extend(entries)
    return merged


def run(
    bom_path: Path,
    symbols_root: Path,
    footprints_root: Path,
    output_path: Path,
    *,
    project_dir: Path | None = None,
) -> dict:
    """Run Stage 2 and write a coverage report. Returns the report dict.

    With ``project_dir``, coverage searches the same roots Stage 5 resolves
    against — the project's own libraries, the shared module library, the
    generated set, then stock. Stock-only coverage was how six installed
    symbols sat invisible while the LLM's paraphrased hints sent every one of
    those parts to a placeholder: coverage is what OUTRANKS the hint in Stage
    5, so a library coverage cannot see is a library the pipeline cannot use.
    """
    bom = schema.load_json(bom_path)
    schema.validate("bom", bom)

    # The doc's explicit pins, applied at read time. Without this, a Symbol
    # or Package cell edited after Stage 1 changes nothing until the LLM
    # stage reruns — coverage kept scoring the stale hints while the design
    # doc plainly said otherwise.
    from . import explicit_pins

    explicit_pins.apply(bom["rows"], project_dir)

    if project_dir is not None:
        from .symbol_resolution import footprint_search_path, search_path

        sym_roots = [r for r, _ in search_path(Path(project_dir), Path(symbols_root))]
        fp_roots = [r for r, _ in footprint_search_path(Path(project_dir), Path(footprints_root))]
    else:
        sym_roots, fp_roots = [Path(symbols_root)], [Path(footprints_root)]

    sym_index = _merged_index(sym_roots, _index_symbols)
    fp_index = _merged_index(fp_roots, _index_footprints)

    rows_out: list[dict] = []
    summary = {"total": 0, "hit": 0, "needs_variant": 0, "miss": 0}

    from . import explicit_pins
    from .symbol_resolution import is_not_placed

    pins = explicit_pins.load(project_dir) if project_dir is not None else {}

    for row in bom["rows"]:
        summary["total"] += 1

        # Two kinds of row where "no match" is the CORRECT state, not a miss.
        # A not_placed part must have no footprint — any match would be wrong
        # copper. And a connector's symbol is generated by Stage 3 from its
        # pinout, so searching stock for one measures nothing — unless the
        # designer pinned an explicit symbol, which is a claim and is held to
        # it. Counting these as misses made stage2 exit nonzero and halt the
        # run over rows that need no action from anyone.
        fp_exempt = is_not_placed(
            row.get("footprint_hint") or row.get("package") or ""
        )
        sym_exempt = (
            str(row.get("local_id", "")).startswith("J")
            and "symbol_hint" not in pins.get(row.get("local_id"), {})
        )

        sym_exact = _lookup_exact(row.get("symbol_hint"), sym_index)
        if sym_exact is not None:
            sym_match: dict | None = _entry_to_match(sym_exact, "exact", None, symbols_root)
        elif (by_mpn := _lookup_mpn(row.get("mpn"), sym_index)) is not None:
            sym_match = _entry_to_match(by_mpn[0], "mpn", by_mpn[1], symbols_root)
        else:
            tokens = _symbol_query_tokens(row)
            fuzzy = _lookup_fuzzy(tokens, sym_index)
            sym_match = _entry_to_match(fuzzy[0], "fuzzy", fuzzy[1], symbols_root) if fuzzy else None

        fp_exact = _lookup_exact(row.get("footprint_hint"), fp_index)
        if fp_exact is not None:
            fp_match: dict | None = _entry_to_match(fp_exact, "exact", None, footprints_root)
        elif (fp_by_mpn := _lookup_mpn(row.get("mpn"), fp_index)) is not None:
            fp_match = _entry_to_match(fp_by_mpn[0], "mpn", fp_by_mpn[1], footprints_root)
        else:
            tokens = _footprint_query_tokens(row)
            fuzzy = _lookup_fuzzy(tokens, fp_index)
            fp_match = _entry_to_match(fuzzy[0], "fuzzy", fuzzy[1], footprints_root) if fuzzy else None

        status = _status_of(
            sym_match, fp_match, sym_exempt=sym_exempt, fp_exempt=fp_exempt
        )
        summary[status] += 1

        sym_query = row.get("symbol_hint") or " ".join(sorted(_symbol_query_tokens(row))) or "(nothing to search with)"
        fp_query = row.get("footprint_hint") or row.get("package") or "(nothing to search with)"
        rows_out.append(
            {
                "local_id": row["local_id"],
                "mpn": row["mpn"],
                # What was searched, kept next to what was found: a
                # needs_variant or miss row must explain itself. The summary
                # counted three misses and named none of them, and the report
                # rows gave no way to see what the lookup had even tried.
                "symbol_query": "(connector — Stage 3 generates its symbol from the pinout)" if sym_exempt else sym_query,
                "footprint_query": "(not_placed — no footprint by design)" if fp_exempt else fp_query,
                "status": status,
                "symbol_match": sym_match,
                "footprint_match": fp_match,
            }
        )

    report = {
        "project_id": bom["project_id"],
        "schema_version": 1,
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "library_roots": {
            "symbols": str(symbols_root),
            "footprints": str(footprints_root),
        },
        "rows": rows_out,
        "summary": summary,
    }
    schema.validate("coverage_report", report)
    schema.dump_json(output_path, report)
    return report


def _status_of(
    sym_match: dict | None,
    fp_match: dict | None,
    *,
    sym_exempt: bool = False,
    fp_exempt: bool = False,
) -> str:
    """Status over the sides that MEAN something for this row.

    An exempt side (a not_placed part's footprint, an unpinned connector's
    symbol) is one where finding nothing is correct, so it neither causes a
    miss nor blocks a hit. A row exempt on both sides is a hit: there is
    nothing anyone could act on.
    """
    sides = [m for m, exempt in ((sym_match, sym_exempt), (fp_match, fp_exempt)) if not exempt]
    if any(m is None for m in sides):
        return "miss"
    if all(m["match_type"] == "exact" for m in sides):
        return "hit"
    return "needs_variant"
