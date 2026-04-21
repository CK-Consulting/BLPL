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


def run(
    bom_path: Path,
    symbols_root: Path,
    footprints_root: Path,
    output_path: Path,
) -> dict:
    """Run Stage 2 and write a coverage report. Returns the report dict."""
    bom = schema.load_json(bom_path)
    schema.validate("bom", bom)

    sym_index = _index_symbols(symbols_root)
    fp_index = _index_footprints(footprints_root)

    rows_out: list[dict] = []
    summary = {"total": 0, "hit": 0, "needs_variant": 0, "miss": 0}

    for row in bom["rows"]:
        summary["total"] += 1

        sym_exact = _lookup_exact(row.get("symbol_hint"), sym_index)
        if sym_exact is not None:
            sym_match: dict | None = _entry_to_match(sym_exact, "exact", None, symbols_root)
        else:
            tokens = _symbol_query_tokens(row)
            fuzzy = _lookup_fuzzy(tokens, sym_index)
            sym_match = _entry_to_match(fuzzy[0], "fuzzy", fuzzy[1], symbols_root) if fuzzy else None

        fp_exact = _lookup_exact(row.get("footprint_hint"), fp_index)
        if fp_exact is not None:
            fp_match: dict | None = _entry_to_match(fp_exact, "exact", None, footprints_root)
        else:
            tokens = _footprint_query_tokens(row)
            fuzzy = _lookup_fuzzy(tokens, fp_index)
            fp_match = _entry_to_match(fuzzy[0], "fuzzy", fuzzy[1], footprints_root) if fuzzy else None

        status = _status_of(sym_match, fp_match)
        summary[status] += 1

        rows_out.append(
            {
                "local_id": row["local_id"],
                "mpn": row["mpn"],
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


def _status_of(sym_match: dict | None, fp_match: dict | None) -> str:
    if sym_match is None or fp_match is None:
        return "miss"
    if sym_match["match_type"] == "exact" and fp_match["match_type"] == "exact":
        return "hit"
    return "needs_variant"
