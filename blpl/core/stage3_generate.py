"""Stage 3: gap-fill generation.

For each row in coverage_report.json that is `miss` or `needs_variant`, Stage 3
either auto-generates a KiCad artifact (when safe and the BOM row has the
required info) or emits an actionable interactive-prompt entry in gaps.json
asking the user to provide the file at a specific path.

Scope of v1 auto-generation:
  - Generic rectangular N-pin symbols (schematic-only; no mechanical risk).
  - No footprints (wrong pad geometry can cause manufacturing failure — we
    ask the user instead).

Off by default; enabled with auto_generate=True. When disabled, every gap
becomes a user prompt regardless of whether it was auto-generatable.

Stage 3 also runs a pin_map classifier across every BOM row
(see :mod:`pipeline.component_classifier`). Auto-resolvable rows (passives,
generic connectors, small-signal 3-pin parts) get their pin_map written back
into bom.json and recorded as an auto-generated gap entry for audit. Rows the
classifier can't handle (FPGAs, MCUs, custom ICs) become interactive user
prompts asking for the pin mapping.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from . import schema, symbol_templates as _syms
from blpl.classifier import component_classifier
from blpl.emitter import loaders as _emitter_loaders


@dataclass
class Stage3Paths:
    generated_symbols_dir: Path   # projects/<proj>/generated/symbols/
    generated_footprints_dir: Path  # projects/<proj>/generated/footprints/<proj>.pretty/
    inputs_dir: Path              # projects/<proj>/inputs/ (where the user drops files)
    gaps_json_path: Path          # projects/<proj>/.pipeline/gaps.json
    gaps_md_path: Path            # projects/<proj>/.pipeline/gaps.md


def paths_for(project_dir: Path) -> Stage3Paths:
    project_dir = Path(project_dir)
    proj_name = project_dir.resolve().name or "project"
    return Stage3Paths(
        generated_symbols_dir=project_dir / "generated" / "symbols",
        generated_footprints_dir=project_dir / "generated" / "footprints" / f"{proj_name}.pretty",
        inputs_dir=project_dir / "inputs",
        gaps_json_path=project_dir / ".pipeline" / "gaps.json",
        gaps_md_path=project_dir / ".pipeline" / "gaps.md",
    )


def _index_bom(bom: dict) -> dict[str, dict]:
    return {r["local_id"]: r for r in bom.get("rows", [])}


def _safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)


def _symbol_filename(local_id: str, mpn: str) -> str:
    return f"{_safe_filename(local_id)}_{_safe_filename(mpn)}.kicad_sym"


def _footprint_filename(local_id: str, mpn: str) -> str:
    return f"{_safe_filename(local_id)}_{_safe_filename(mpn)}.kicad_mod"


def _has_symbol_match(row: dict) -> bool:
    sm = row.get("symbol_match")
    return sm is not None and sm.get("match_type") == "exact"


def _has_footprint_match(row: dict) -> bool:
    fm = row.get("footprint_match")
    return fm is not None and fm.get("match_type") == "exact"


def _autogen_symbol(
    local_id: str,
    bom_row: dict,
    paths: Stage3Paths,
) -> tuple[Path, str] | None:
    """Auto-generate a generic rectangular symbol. Returns (path, library_ref) or None
    if pin_count is missing (can't build a symbol without it).
    """
    pin_count = bom_row.get("pin_count")
    if not pin_count or pin_count < 1:
        return None
    paths.generated_symbols_dir.mkdir(parents=True, exist_ok=True)
    fname = _symbol_filename(local_id, bom_row["mpn"])
    out_path = paths.generated_symbols_dir / fname
    prefix = bom_row.get("refdes", local_id)[0] if bom_row.get("refdes") else "U"
    if prefix not in ("U", "J", "X", "Y"):
        prefix = "U"
    spec = _syms.SymbolSpec(
        name=bom_row["mpn"],
        pin_count=pin_count,
        reference_prefix=prefix,
        description=bom_row.get("description", "")[:120],
    )
    out_path.write_text(_syms.render(spec), encoding="utf-8")
    # Library reference: the file is a single-symbol library; convention is filename-stem as lib name.
    lib_name = f"hdm_generated_{_safe_filename(Path(paths.generated_symbols_dir).parent.parent.name)}"
    return out_path, f"{lib_name}:{bom_row['mpn']}"


def _prompt_for_symbol(local_id: str, bom_row: dict, paths: Stage3Paths) -> dict:
    suggested = paths.inputs_dir / _symbol_filename(local_id, bom_row["mpn"])
    rel = _project_rel(suggested)
    return {
        "local_id": local_id,
        "mpn": bom_row["mpn"],
        "kind": "symbol",
        "status": "miss",
        "auto_generated": False,
        "user_prompt": (
            f"No KiCad symbol found for {bom_row['mpn']} ({bom_row.get('description','')}). "
            f"Please provide a .kicad_sym at `{rel}` — or update bom.json row "
            f"`local_id={local_id}` with a `symbol_hint` of `Lib:Name` pointing to an existing symbol."
        ),
        "suggested_input_path": str(rel),
    }


def _prompt_for_footprint(local_id: str, bom_row: dict, paths: Stage3Paths) -> dict:
    suggested = paths.inputs_dir / _footprint_filename(local_id, bom_row["mpn"])
    rel = _project_rel(suggested)
    pkg = bom_row.get("package", "")
    return {
        "local_id": local_id,
        "mpn": bom_row["mpn"],
        "kind": "footprint",
        "status": "miss",
        "auto_generated": False,
        "user_prompt": (
            f"No KiCad footprint found for {bom_row['mpn']} "
            f"(package '{pkg}'). Please provide a .kicad_mod at `{rel}` — or update "
            f"bom.json row `local_id={local_id}` with a `footprint_hint` pointing "
            f"to an existing footprint library entry. "
            f"Phase 4 v1 deliberately does NOT auto-generate footprints; wrong pad "
            f"geometry can cause manufacturing failure."
        ),
        "suggested_input_path": str(rel),
    }


class ResolvePinMapError(ValueError):
    """Raised when resolve_pin_map can't produce a valid pin_map."""


def _pin_map_from_csv(csv_path: Path) -> dict[str, str]:
    """Parse a pinmap CSV into ``{signal: pin_number}``.

    Accepts two header shapes:
      - ``signal,pin`` (or ``name,number``) — logical-first, the form the user
        would paste from a datasheet table.
      - ``pin,signal`` (or ``number,name``) — pin-first, the form a KiCad
        library export tends to produce.

    Blank rows and ``#``-prefixed comment lines are ignored. Quoted CSV is
    accepted. The first row is treated as a header if either of its cells is
    a known column name; otherwise it's treated as data and the column order
    is assumed to be ``signal,pin``.
    """
    import csv

    _HEADERS_SIGNAL_FIRST = {"signal", "name", "net", "logical"}
    _HEADERS_PIN_FIRST = {"pin", "number", "num", "physical"}

    with csv_path.open(newline="") as f:
        reader = csv.reader(f)
        rows: list[list[str]] = []
        for raw in reader:
            if not raw:
                continue
            cells = [c.strip() for c in raw]
            if cells[0].startswith("#"):
                continue
            if not any(cells):
                continue
            rows.append(cells)

    if not rows:
        raise ResolvePinMapError(f"{csv_path} is empty or contains no data rows")

    # Header detection.
    first = [c.lower() for c in rows[0]]
    signal_first = True
    data_rows = rows[1:] if (set(first) & (_HEADERS_SIGNAL_FIRST | _HEADERS_PIN_FIRST)) else rows
    if len(first) >= 2 and first[0] in _HEADERS_PIN_FIRST and first[1] in _HEADERS_SIGNAL_FIRST:
        signal_first = False

    out: dict[str, str] = {}
    for i, row in enumerate(data_rows, start=1):
        if len(row) < 2:
            raise ResolvePinMapError(
                f"{csv_path}:{i}: expected at least 2 columns, got {row!r}"
            )
        a, b = row[0], row[1]
        if signal_first:
            signal, pin = a, b
        else:
            pin, signal = a, b
        if not signal or not pin:
            continue
        out[signal] = pin
    if not out:
        raise ResolvePinMapError(
            f"{csv_path}: no usable (signal, pin) rows found"
        )
    return out


def _pin_map_from_lib_symbol(
    lib_symbol: str, symbols_root: Path
) -> tuple[dict[str, str], list[str]]:
    """Derive ``{signal: pin_number}`` from a KiCad library symbol's pin-name table.

    Identical behaviour to the classifier's internal helper, promoted here so
    the resolve-pin-map command can reuse it without taking a full classifier
    dependency on the BOM row shape.
    """
    return component_classifier._pin_map_from_symbol(
        lib_symbol, symbols_root=symbols_root
    )


def resolve_pin_map(
    local_id: str,
    bom_path: Path,
    *,
    lib_symbol: str | None = None,
    csv_path: Path | None = None,
    symbols_root: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Overwrite one BOM row's pin_map from a library symbol or a CSV.

    Exactly one of ``lib_symbol`` or ``csv_path`` must be provided.

    Returns a summary: ``{"local_id", "pin_map", "source", "warnings", "persisted": bool}``.
    """
    if bool(lib_symbol) == bool(csv_path):
        raise ResolvePinMapError(
            "resolve_pin_map requires exactly one of lib_symbol or csv_path"
        )

    bom = schema.load_json(bom_path)
    schema.validate("bom", bom)
    target = next((r for r in bom["rows"] if r["local_id"] == local_id), None)
    if target is None:
        raise ResolvePinMapError(
            f"local_id {local_id!r} not found in {bom_path} "
            f"(known: {sorted(r['local_id'] for r in bom['rows'])[:10]}…)"
        )

    warnings: list[str] = []
    if lib_symbol:
        if symbols_root is None:
            symbols_root = Path(__file__).resolve().parent.parent / "kicad-symbols"
        pin_map, warnings = _pin_map_from_lib_symbol(lib_symbol, Path(symbols_root))
        if not pin_map:
            raise ResolvePinMapError(
                f"could not derive a pin_map from {lib_symbol!r}: {warnings or 'no pins extracted'}"
            )
        source = "connector_lookup"
        # Also seed symbol_hint if the user hadn't set one.
        if not target.get("symbol_hint"):
            target["symbol_hint"] = lib_symbol
    else:
        pin_map = _pin_map_from_csv(Path(csv_path))
        source = "user_provided"

    target["pin_map"] = pin_map
    target["pin_map_source"] = source

    persisted = False
    if not dry_run:
        schema.validate("bom", bom)
        schema.dump_json(bom_path, bom)
        persisted = True

    return {
        "local_id": local_id,
        "pin_map": pin_map,
        "source": source,
        "warnings": warnings,
        "persisted": persisted,
    }


def _project_rel(path: Path) -> Path:
    """Render path relative to cwd if possible, else return as-is."""
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def _symbol_exists(ref: str, symbols_root) -> bool:
    """Whether a Lib:Name reference resolves in any of the given roots."""
    try:
        _emitter_loaders._load_symbol_node(ref, symbols_root)
        return True
    except _emitter_loaders.LibraryMiss:
        return False


def _classify_and_resolve_pin_map(
    bom_row: dict,
    paths: Stage3Paths,
    *,
    symbols_root: Path,
    footprints_root: Path,
    doc_pin_maps: dict[str, dict[str, str]] | None = None,
) -> dict | None:
    """Run the component classifier against a BOM row.

    If the classifier auto-resolves the row, mutates ``bom_row`` to carry the
    resolved ``pin_map`` (and fills in missing symbol_hint/footprint_hint) and
    returns an audit-trail gap entry. If the row is classified ``specific``,
    returns a user-prompt gap entry telling the user how to supply pin_map.

    Returns ``None`` when the row already has a pin_map (nothing to do).
    """
    if bom_row.get("pin_map"):
        return None

    from .symbol_resolution import is_not_placed

    if is_not_placed(bom_row.get("footprint_hint") or bom_row.get("package") or ""):
        # Never on the board, never in the netlist: there are no pins for a
        # pin_map to describe — the same exemption doctor's DOC-011 applies.
        return None

    # The design doc's own pinout table, before any classifier guessing: it
    # is the durable statement of signal→pin (DOC-011 tells people to write
    # exactly this table), where bom.json's pin_map is a regenerated
    # artifact one Stage 1 rerun wipes. What a rerun destroys, this
    # re-derives.
    doc_map = (doc_pin_maps or {}).get(bom_row["local_id"])
    if doc_map:
        bom_row["pin_map"] = dict(doc_map)
        bom_row["pin_map_source"] = "design_artifact"
        return {
            "local_id": bom_row["local_id"],
            "mpn": bom_row["mpn"],
            "kind": "pin_map",
            "status": "resolved",
            "auto_generated": True,
            "user_prompt": (
                f"Resolved pin_map for {bom_row['local_id']} ({bom_row['mpn']}) from its "
                f"pinout table in the design doc.\n  pin_map entries={len(doc_map)}"
            ),
            "notes": "design_artifact",
            "pin_map_resolution": _clean_resolution(
                source="design_artifact", pin_map=doc_map
            ),
        }

    # An explicitly pinned symbol, before any classifier guessing: gap
    # prompts have always told the user to run resolve-pin-map --lib-symbol
    # against exactly this reference ("fastest; derives signal→pin from a
    # KiCad symbol's pin-name table"). When the designer already named the
    # symbol in the doc, asking them to type its name back is a form with
    # one field and one possible answer — derive it.
    sym_hint = bom_row.get("symbol_hint") or ""
    if ":" in sym_hint:
        pin_map, warns = component_classifier._pin_map_from_symbol(
            sym_hint, symbols_root, pin_count_hint=bom_row.get("pin_count")
        )
        if pin_map:
            bom_row["pin_map"] = pin_map
            bom_row["pin_map_source"] = "pinned_symbol"
            prompt = [
                f"Resolved pin_map for {bom_row['local_id']} ({bom_row['mpn']}) from its "
                f"pinned symbol {sym_hint}.\n  pin_map entries={len(pin_map)}"
            ]
            if warns:
                prompt.append("  warnings: " + "; ".join(warns))
            return {
                "local_id": bom_row["local_id"],
                "mpn": bom_row["mpn"],
                "kind": "pin_map",
                "status": "resolved",
                "auto_generated": True,
                "user_prompt": "\n".join(prompt),
                "notes": "pinned_symbol",
                "pin_map_resolution": _clean_resolution(
                    source="pinned_symbol", lib_symbol=sym_hint, pin_map=pin_map
                ),
            }

    result = component_classifier.classify(
        bom_row, symbols_root=symbols_root, footprints_root=footprints_root
    )

    local_id = bom_row["local_id"]
    mpn = bom_row["mpn"]
    if result.is_auto_resolved:
        # Write back to bom_row (caller persists to bom.json).
        bom_row["pin_map"] = result.pin_map or {}
        bom_row["pin_map_source"] = result.source
        if not bom_row.get("symbol_hint") and result.lib_symbol:
            bom_row["symbol_hint"] = result.lib_symbol
        if not bom_row.get("footprint_hint") and result.lib_footprint:
            bom_row["footprint_hint"] = result.lib_footprint
        prompt_lines = [
            f"Auto-resolved pin_map for {local_id} ({mpn}) via {result.source}.",
            f"  symbol={result.lib_symbol}",
            f"  footprint={result.lib_footprint or '(user choice)'}",
            f"  pin_map entries={len(result.pin_map or {})}",
        ]
        if result.warnings:
            prompt_lines.append("  warnings: " + "; ".join(result.warnings))
        return {
            "local_id": local_id,
            "mpn": mpn,
            "kind": "pin_map",
            "status": "resolved",
            "auto_generated": True,
            "user_prompt": "\n".join(prompt_lines),
            "notes": result.reason,
            "pin_map_resolution": _clean_resolution(
                source=result.source,
                lib_symbol=result.lib_symbol,
                lib_footprint=result.lib_footprint,
                pin_map=result.pin_map,
                reason=result.reason,
            ),
        }
    # specific — user must provide pin_map.
    return {
        "local_id": local_id,
        "mpn": mpn,
        "kind": "pin_map",
        "status": "miss",
        "auto_generated": False,
        "user_prompt": (
            f"No classifier bucket matched {local_id} ({mpn}, package={bom_row.get('package','?')}). "
            f"Resolve its pin_map one of these ways:\n"
            f"  A) `hdm-pipeline resolve-pin-map --project-dir <proj> --local-id {local_id} "
            f"--lib-symbol Lib:Name` — fastest; derives signal→pin from a KiCad symbol's pin-name table.\n"
            f"  B) `hdm-pipeline resolve-pin-map --project-dir <proj> --local-id {local_id} "
            f"--csv path/to/pinmap.csv` — for datasheet pastes; CSV header must be "
            f"`signal,pin` (or `pin,signal`).\n"
            f"  C) Edit bom.json row for `local_id={local_id}` directly; set a `pin_map` object "
            f"mapping logical signal names to physical pin numbers."
        ),
        "notes": result.reason,
        "pin_map_resolution": _clean_resolution(
            source="specific_needs_user", reason=result.reason
        ),
    }


def _clean_resolution(
    *,
    source: str,
    lib_symbol: str | None = None,
    lib_footprint: str | None = None,
    pin_map: dict[str, str] | None = None,
    reason: str = "",
) -> dict:
    """Assemble a pin_map_resolution object without null-valued keys (schema rejects them)."""
    out: dict = {"source": source}
    if lib_symbol:
        out["lib_symbol"] = lib_symbol
    if lib_footprint:
        out["lib_footprint"] = lib_footprint
    if pin_map:
        out["pin_map"] = dict(pin_map)
    if reason:
        out["reason"] = reason
    return out


def run(
    coverage_path: Path,
    bom_path: Path,
    project_dir: Path,
    *,
    auto_generate: bool = False,
    symbols_root: Path | None = None,
    footprints_root: Path | None = None,
    write_bom: bool = True,
) -> dict:
    """Produce gaps.json + gaps.md. When auto_generate=True, also write generated symbols.

    Runs the pin_map classifier on every BOM row regardless of ``auto_generate``
    — classification is always safe (it doesn't emit KiCad files). With
    ``write_bom=True`` (default) the classifier's resolutions are persisted
    back to bom.json so Stage 5 picks them up.
    """
    coverage = schema.load_json(coverage_path)
    schema.validate("coverage_report", coverage)
    bom = schema.load_json(bom_path)
    schema.validate("bom", bom)
    bom_idx = _index_bom(bom)

    paths = paths_for(project_dir)
    paths.gaps_json_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve library roots the way Stage 5 resolves them: project libraries
    # first, then modules, generated, stock. Two bugs lived here. The old
    # fallback computed pipeline_root/"kicad-symbols" — one directory short
    # of the repo root, a path that exists nowhere — so every classifier
    # symbol lookup raised LibraryMiss and every connector fell through to
    # "specific": 19 hand-prompts for parts the connector bucket handles
    # fine. And stock-only would repeat Stage 2's old blindness: a connector
    # symbol the project owns must be usable for pin_map derivation too.
    from .stage6_compile_kicad import _DEFAULT_FOOTPRINTS, _DEFAULT_SYMBOLS
    from .symbol_resolution import footprint_search_path, search_path

    stock_syms = Path(symbols_root) if symbols_root else _DEFAULT_SYMBOLS
    stock_fps = Path(footprints_root) if footprints_root else _DEFAULT_FOOTPRINTS
    symbols_root = [r for r, _ in search_path(Path(project_dir), stock_syms)]
    footprints_root = [r for r, _ in footprint_search_path(Path(project_dir), stock_fps)]

    gaps: list[dict] = []
    for cov_row in coverage["rows"]:
        if cov_row["status"] == "hit":
            continue
        bom_row = bom_idx.get(cov_row["local_id"])
        if bom_row is None:
            continue

        # Symbol gap?
        if not _has_symbol_match(cov_row):
            if auto_generate:
                result = _autogen_symbol(cov_row["local_id"], bom_row, paths)
                if result is not None:
                    gen_path, lib_ref = result
                    gaps.append(
                        {
                            "local_id": cov_row["local_id"],
                            "mpn": cov_row["mpn"],
                            "kind": "symbol",
                            "status": cov_row["status"],
                            "auto_generated": True,
                            "generated_path": str(_project_rel(gen_path)),
                            "user_prompt": (
                                f"Auto-generated a generic rectangular symbol at `{_project_rel(gen_path)}`. "
                                f"This is a schematic placeholder — consider replacing with a datasheet-accurate "
                                f"symbol before tape-out. To use it, set `symbol_hint: {lib_ref}` on bom.json row "
                                f"`local_id={cov_row['local_id']}`."
                            ),
                            "notes": "generic_rectangle_symbol",
                        }
                    )
                else:
                    # Couldn't auto-gen (missing pin_count). Fall through to user prompt.
                    gaps.append(_prompt_for_symbol(cov_row["local_id"], bom_row, paths))
            else:
                gaps.append(_prompt_for_symbol(cov_row["local_id"], bom_row, paths))

        # Footprint gap? Never auto-generated in v1.
        if not _has_footprint_match(cov_row):
            gaps.append(_prompt_for_footprint(cov_row["local_id"], bom_row, paths))

    # Classify every BOM row for pin_map, not just those with coverage misses —
    # an FPGA may have a library-matched symbol but still need a pin_map.
    from . import explicit_pins

    explicit_pins.apply(bom["rows"], project_dir)
    doc_pin_maps = explicit_pins.pin_maps(project_dir)
    bom_mutated = False
    for bom_row in bom["rows"]:
        # A connector hint that names no symbol anywhere is the LLM's
        # invention, and Stage 5 turns it into a "NO REAL SYMBOL" placeholder
        # for a part whose whole point is being standard — a 40-pin FFC is
        # generic BY DESIGN. The connector bucket already knows the right
        # stock symbol; it just refused to overwrite an existing hint. An
        # existing hint that resolves is respected (the designer may have
        # pinned it); one that resolves to nothing defends nothing.
        sym_hint = bom_row.get("symbol_hint") or ""
        if ":" in sym_hint and not _symbol_exists(sym_hint, symbols_root):
            repaired = component_classifier._classify_connector(bom_row, symbols_root)
            if (
                repaired is not None
                and repaired.lib_symbol
                and _symbol_exists(repaired.lib_symbol, symbols_root)
            ):
                bom_row["symbol_hint"] = repaired.lib_symbol
                bom_mutated = True
                gaps.append(
                    {
                        "local_id": bom_row["local_id"],
                        "mpn": bom_row["mpn"],
                        "kind": "symbol",
                        "status": "resolved",
                        "auto_generated": True,
                        "user_prompt": (
                            f"Replaced symbol hint '{sym_hint}' — it names no symbol in any "
                            f"library — with {repaired.lib_symbol} ({repaired.reason})."
                        ),
                    }
                )
        pin_map_gap = _classify_and_resolve_pin_map(
            bom_row,
            paths,
            symbols_root=symbols_root,
            footprints_root=footprints_root,
            doc_pin_maps=doc_pin_maps,
        )
        if pin_map_gap is not None:
            gaps.append(pin_map_gap)
            if pin_map_gap["auto_generated"]:
                bom_mutated = True

    if bom_mutated and write_bom:
        schema.validate("bom", bom)  # ensure our additions still pass schema
        schema.dump_json(bom_path, bom)

    out = {
        "project_id": coverage["project_id"],
        "schema_version": 1,
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "gaps": gaps,
    }
    schema.validate("gaps", out)
    schema.dump_json(paths.gaps_json_path, out)
    paths.gaps_md_path.write_text(_render_markdown(out), encoding="utf-8")
    return out


def _render_markdown(gaps: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Coverage gaps for {gaps['project_id']}\n")
    lines.append(f"_Generated {gaps['generated_at']}._ **{len(gaps['gaps'])} gap(s).**\n")

    auto = [g for g in gaps["gaps"] if g["auto_generated"]]
    pending = [g for g in gaps["gaps"] if not g["auto_generated"]]

    if auto:
        lines.append("## Auto-generated\n")
        for g in auto:
            lines.append(f"### {g['local_id']} ({g['mpn']}) — {g['kind']}")
            lines.append(g["user_prompt"])
            lines.append("")
    if pending:
        lines.append("## Needs your attention\n")
        for g in pending:
            lines.append(f"### {g['local_id']} ({g['mpn']}) — {g['kind']} ({g['status']})")
            lines.append(g["user_prompt"])
            lines.append("")
    return "\n".join(lines) + "\n"
