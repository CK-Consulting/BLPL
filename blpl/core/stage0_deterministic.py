"""Stage 0 (deterministic variant): Markdown -> design_artifact.v1 via pipe-table extraction.

Port of dev.03-base-station/parse_design_files.py adapted to emit the pipeline's
design_artifact.v1 schema. Pure stdlib + pipeline.markdown_tables.

No LLM. Always available. Used as a baseline that the LLM variant is compared
against in stage0_compare.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import markdown_tables as _md
from . import schema


# Patterns for finding connector/component refdes in headings that precede pinout tables.
_CONNECTOR_HEADING_RE = re.compile(r"^#+\s*[\d.]*\s*(J[\w_]*\d*)[:\s(]", re.MULTILINE)

# Reference designator pattern on bullet lines: J2, U1, J_ETH, J_HALOW, etc.
_REFDES_BULLET_RE = re.compile(r"\b(J[\w_]*\d+|U\d+|J_[A-Z][A-Z_]*)\b")


def _column_lookup(row: dict[str, str], candidates: list[str]) -> str:
    """Return the value of the first matching column (case-insensitive, substring)."""
    lower_row = {k.lower(): v for k, v in row.items()}
    for cand in candidates:
        c = cand.lower()
        if c in lower_row:
            return lower_row[c]
        for k, v in lower_row.items():
            if c in k:
                return v
    return ""


def _normalize_pin_headers(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for h in headers:
        hl = h.lower().strip()
        if hl == "pin" or "pin" in hl and "signal" not in hl:
            mapping[h] = "pin"
        elif "signal" in hl:
            mapping[h] = "signal"
        elif hl in ("direction", "function"):
            mapping[h] = "function"
        elif "voltage" in hl:
            mapping[h] = "voltage"
        else:
            mapping[h] = h
    return mapping


def _find_connector_for_table(text: str, table: _md.ParsedTable) -> str:
    """Return the connector refdes heading that most closely precedes the table,
    or 'UNKNOWN' if none is found.

    Uses the table's 1-based line_start (which is unambiguous even when multiple
    tables share identical header rows).
    """
    lines = text.splitlines(keepends=True)
    if table.line_start < 1 or table.line_start > len(lines):
        return "UNKNOWN"
    tbl_offset = sum(len(line) for line in lines[: table.line_start - 1])
    best_ref = "UNKNOWN"
    for m in _CONNECTOR_HEADING_RE.finditer(text):
        if m.start() <= tbl_offset:
            best_ref = m.group(1)
        else:
            break
    return best_ref


def _make_source_ref(table: _md.ParsedTable) -> dict:
    return {
        "file": table.source_file,
        "line_start": table.line_start,
        "line_end": table.line_end,
    }


def extract(md_files: list[Path]) -> dict:
    """Run deterministic extraction over the given Markdown files.

    Returns a design_artifact.v1 dict. Source_file fields use the file as passed
    in (caller controls absolute vs relative).
    """
    components: dict[str, dict] = {}       # keyed by local_id, merged as tables are seen
    connectors: dict[str, dict] = {}       # keyed by local_id (refdes)
    raw_nets: list[dict] = []              # not filled by deterministic stage 0 — LLM's job
    subsystems: list[dict] = []            # not filled by deterministic stage 0 — LLM's job

    source_files: list[str] = []
    for md_path in md_files:
        md_path = Path(md_path)
        if not md_path.exists():
            continue
        text = md_path.read_text(encoding="utf-8")
        tables = _md.extract_tables_from_file(md_path)
        # Track the source_file string as the parser recorded it (relative if possible).
        if tables:
            source_files.append(tables[0].source_file)
        else:
            try:
                source_files.append(str(md_path.resolve().relative_to(Path.cwd())))
            except ValueError:
                source_files.append(str(md_path.resolve()))

        for table in tables:
            kind = _md.classify(table)
            if kind == "bom":
                _absorb_bom_table(table, components)
            elif kind == "pinout":
                connector_ref = _find_connector_for_table(text, table)
                _absorb_pinout_table(table, connector_ref, connectors)

    return {
        "project_id": _infer_project_id(md_files),
        "schema_version": 1,
        "source_files": source_files,
        "components": list(components.values()),
        "connectors": list(connectors.values()),
        "subsystems": subsystems,
        "raw_nets": raw_nets,
    }


def _infer_project_id(md_files: list[Path]) -> str:
    if not md_files:
        return "unknown-project"
    parent = Path(md_files[0]).resolve().parent.name
    return parent.replace(".", "-")


def _absorb_bom_table(table: _md.ParsedTable, components: dict[str, dict]) -> None:
    source_ref = _make_source_ref(table)
    for row in table.rows:
        ref = _column_lookup(row, ["Ref", "Reference"]).strip()
        if not ref:
            continue
        description = _column_lookup(row, ["Description"])
        package_hint = _column_lookup(row, ["Package"])
        part_hint = _column_lookup(row, ["Part Number", "MPN", "Part"])
        manufacturer_hint = _column_lookup(row, ["Manufacturer"])
        comp = components.setdefault(
            ref,
            {"local_id": ref, "description": description or ref, "source_ref": source_ref},
        )
        if description and "description" not in comp:
            comp["description"] = description
        if package_hint:
            comp["package_hint"] = package_hint
        if part_hint:
            comp["part_hint"] = part_hint
        if manufacturer_hint:
            comp["manufacturer_hint"] = manufacturer_hint


def _absorb_pinout_table(
    table: _md.ParsedTable, connector_ref: str, connectors: dict[str, dict]
) -> None:
    header_map = _normalize_pin_headers(table.headers)
    pins: list[dict] = []
    for row in table.rows:
        pin_val = ""
        signal_val = ""
        function_val = ""
        voltage_val = ""
        for orig_h, norm_h in header_map.items():
            val = row.get(orig_h, "").strip()
            if not val:
                continue
            if norm_h == "pin":
                pin_val = val
            elif norm_h == "signal":
                signal_val = val
            elif norm_h == "function":
                function_val = val
            elif norm_h == "voltage":
                voltage_val = val
        if not pin_val or not signal_val:
            continue
        pin_entry: dict = {"pin": pin_val, "signal": signal_val}
        if function_val:
            pin_entry["function"] = function_val
        if voltage_val:
            pin_entry["voltage"] = voltage_val
        pins.append(pin_entry)

    if not pins:
        return
    entry = connectors.get(connector_ref)
    if entry is None:
        connectors[connector_ref] = {
            "local_id": connector_ref,
            "pins": pins,
            "pin_count": len(pins),
            "source_ref": _make_source_ref(table),
        }
    else:
        # Append pins (handles multi-table connectors).
        entry["pins"].extend(pins)
        entry["pin_count"] = len(entry["pins"])


def run(md_files: list[Path], output_path: Path) -> dict:
    """Extract from md_files and write a schema-validated design_artifact.json."""
    artifact = extract([Path(p) for p in md_files])
    schema.validate("design_artifact", artifact)
    schema.dump_json(output_path, artifact)
    return artifact
