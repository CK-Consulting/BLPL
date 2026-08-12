"""Stage 0 (deterministic variant): Markdown -> design_artifact.v1 via pipe-table extraction.

Port of dev.03-base-station/parse_design_files.py adapted to emit the pipeline's
design_artifact.v1 schema. Pure stdlib + pipeline.markdown_tables.

No LLM. Always available. Used as a baseline that the LLM variant is compared
against in stage0_compare.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import markdown_tables as _md
from . import modules_remix as _modules_remix
from . import schema


# Patterns for finding connector/component refdes in headings that precede pinout tables.
#
# The old pattern was r"^#+\s*[\d.]*\s*(J[\w_]*\d*)[:\s(]", which required the refdes
# to sit *immediately* after the heading marker and to start with J. That silently
# dropped two things people actually write, including the form SKILL.md documents:
#
#   "## Connector J_USB_C — USB-C receptacle"   (leading word before the refdes)
#   "## U_GNSS (LC76G-PA) pinout"               (a pinout for an IC, not a connector)
#
# A dropped pinout table produces no error — the pins simply never become nets. So
# instead of anchoring on position, scan the heading for a refdes-shaped token.
_HEADING_RE = re.compile(r"^#+[^\n]*", re.MULTILINE)

# A refdes is a J (connector) or U (IC) followed by either a number (J2, U1) or an
# underscore-qualified name (J_USB_C, U_GNSS). Requiring the digit-or-underscore is
# what keeps ordinary prose out: "USB" and "Connector" are not refdes, but "U_GNSS"
# and "J1" are.
_REFDES_IN_HEADING_RE = re.compile(r"\b((?:J|U)(?:_[A-Za-z0-9]\w*|\d+\w*))\b")


def refdes_in_heading(heading: str) -> str | None:
    """Return the first refdes-shaped token in a heading line, or None."""
    m = _REFDES_IN_HEADING_RE.search(heading)
    return m.group(1) if m else None

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


# The local_id given to a pinout table with no refdes heading above it. Callers
# suffix it (_1, _2, …) so two unanchored tables never merge into one part.
_UNANCHORED = "UNKNOWN"


def _heading_text_above(text: str, line_start: int) -> str | None:
    """The nearest heading line above a table, stripped of its '#' marks."""
    lines = text.splitlines()
    for i in range(min(line_start, len(lines)) - 1, -1, -1):
        stripped = lines[i].lstrip()
        if stripped.startswith("#"):
            return stripped.lstrip("# ").strip() or None
    return None


def _find_connector_for_table(text: str, table: _md.ParsedTable) -> str:
    """Return the connector refdes heading that most closely precedes the table,
    or ``_UNANCHORED`` if none is found.

    Uses the table's 1-based line_start (which is unambiguous even when multiple
    tables share identical header rows).
    """
    lines = text.splitlines(keepends=True)
    if table.line_start < 1 or table.line_start > len(lines):
        return _UNANCHORED
    tbl_offset = sum(len(line) for line in lines[: table.line_start - 1])
    best_ref = _UNANCHORED
    for m in _HEADING_RE.finditer(text):
        if m.start() > tbl_offset:
            break
        # Headings without a refdes (e.g. "## BOM — Core Subsystem") don't reset the
        # anchor; only a refdes-bearing heading rebinds it.
        ref = refdes_in_heading(m.group(0))
        if ref:
            best_ref = ref
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
    gpio_assignments: list[dict] = []      # host GPIO → signal rows, consumed by Stage 4
    module_names: list[str] = []           # reusable blocks named in a "Modules" section
    warnings: list[dict] = []
    unanchored_seen = 0

    source_files: list[str] = []
    for md_path in md_files:
        md_path = Path(md_path)
        if not md_path.exists():
            continue
        text = md_path.read_text(encoding="utf-8")
        for name in _modules_remix.parse_modules_section(text):
            if name not in module_names:
                module_names.append(name)
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
                if connector_ref == _UNANCHORED:
                    # Every unanchored table used to land on the single local_id
                    # "UNKNOWN", so a power header and an audio jack merged into one
                    # bogus part carrying VBUS, GND, TIP and RING — silently, and it
                    # reached the board. Give each its own id so unrelated pinouts can
                    # never fuse, and say so.
                    unanchored_seen += 1
                    connector_ref = f"{_UNANCHORED}_{unanchored_seen}"
                    heading = _heading_text_above(text, table.line_start)
                    label = heading or "(no heading)"
                    warnings.append(
                        {
                            "code": "STAGE0-001",
                            "summary": (
                                f'Pinout table under "{label}" has no refdes to anchor to; '
                                f"its pins were kept under the placeholder {connector_ref}."
                            ),
                            "fix": (
                                "Put the refdes in the heading, e.g. '## J_USB_C — USB-C "
                                "receptacle' or '## U_GNSS (LC76G-PA) pinout'. A refdes is J or U "
                                "followed by a number (J2) or an underscore name (J_USB_C)."
                            ),
                            "local_id": connector_ref,
                            "source_ref": _make_source_ref(table),
                        }
                    )
                _absorb_pinout_table(table, connector_ref, connectors, warnings)
            elif kind == "gpio":
                # A GPIO map records which HOST pin drives each signal, so it needs
                # a host refdes — and it must come from the table's own heading, not
                # the pinout-style "nearest refdes heading above" anchor: on a real
                # doc the nearest refdes heading was the GNSS chip's pinout, which
                # would have bound every host GPIO to the wrong part.
                heading = _heading_text_above(text, table.line_start)
                host = refdes_in_heading(heading) if heading else None
                if host is None:
                    label = heading or "(no heading)"
                    warnings.append(
                        {
                            "code": "STAGE0-005",
                            "summary": (
                                f'GPIO map under "{label}" names no host refdes in its '
                                "heading; its rows were not consumed."
                            ),
                            "fix": (
                                "Put the host's refdes in the GPIO table's heading, e.g. "
                                "'## U_MCU GPIO assignment'. Stage 4 then joins each GPIO "
                                "pin into the named signal's net."
                            ),
                            "source_ref": _make_source_ref(table),
                        }
                    )
                else:
                    _absorb_gpio_table(table, host, gpio_assignments)
            else:
                # Classified as neither BOM nor pinout, so nothing reads it. Often
                # that is right (a file index, a prose table), but on a real design
                # it also swallowed the net-classes table and a GPIO map — design
                # intent the user wrote and the pipeline ignored without a word.
                # Listing the columns is what makes this triageable at a glance.
                warnings.append(
                    {
                        "code": "STAGE0-004",
                        "summary": (
                            f"Table ignored — classified as neither BOM nor pinout "
                            f"(columns: {', '.join(table.headers) or 'none'})."
                        ),
                        "fix": (
                            "Harmless for prose and index tables. If this table carries "
                            "design intent, restate it in a form Stage 0 reads: a BOM table "
                            "(Ref/Description/MPN…), a pinout table (Pin/Signal), or a GPIO "
                            "assignment map (GPIO/Signal) — the latter two under a heading "
                            "naming their refdes."
                        ),
                        "source_ref": _make_source_ref(table),
                    }
                )

    artifact = {
        "project_id": _infer_project_id(md_files),
        "schema_version": 1,
        "source_files": source_files,
        "components": list(components.values()),
        "connectors": list(connectors.values()),
        "subsystems": subsystems,
        "raw_nets": raw_nets,
    }
    if gpio_assignments:
        artifact["gpio_assignments"] = gpio_assignments
    if warnings:
        artifact["warnings"] = warnings
    if module_names:
        # Modules are resolved relative to the design document, which is the
        # only project location this stage knows about.
        _modules_remix.expand(artifact, Path(md_files[0]).resolve().parent, module_names)
    return artifact


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
    table: _md.ParsedTable,
    connector_ref: str,
    connectors: dict[str, dict],
    warnings: list[dict] | None = None,
) -> None:
    header_map = _normalize_pin_headers(table.headers)
    pins: list[dict] = []
    skipped_rows = 0
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
            # A row missing either half cannot become a net. Counted, not narrated
            # per-row: a 100-pin BGA with a blank column would otherwise bury the
            # report in a hundred identical lines.
            skipped_rows += 1
            continue
        pin_entry: dict = {"pin": pin_val, "signal": signal_val}
        if function_val:
            pin_entry["function"] = function_val
        if voltage_val:
            pin_entry["voltage"] = voltage_val
        pins.append(pin_entry)

    if warnings is not None and skipped_rows:
        warnings.append(
            {
                "code": "STAGE0-002",
                "summary": (
                    f"{connector_ref}: {skipped_rows} pinout row(s) skipped — each was "
                    "missing a pin number or a signal name."
                ),
                "fix": (
                    "Give every row both a pin and a signal. Blank signals are not treated "
                    "as no-connects; the row is dropped and the pin never becomes a net."
                ),
                "local_id": connector_ref,
                "source_ref": _make_source_ref(table),
            }
        )

    if not pins:
        # A table the classifier called a pinout that yielded nothing is almost
        # always a column-naming mismatch, and it is exactly the failure that used
        # to pass in total silence.
        if warnings is not None:
            warnings.append(
                {
                    "code": "STAGE0-003",
                    "summary": (
                        f"{connector_ref}: pinout table produced no usable pins "
                        f"(columns: {', '.join(table.headers) or 'none'})."
                    ),
                    "fix": (
                        "Stage 0 needs a column matching 'Pin' and one matching 'Signal'. "
                        "Rename the columns to match, or the whole table is ignored."
                    ),
                    "local_id": connector_ref,
                    "source_ref": _make_source_ref(table),
                }
            )
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


def _absorb_gpio_table(
    table: _md.ParsedTable, host: str, gpio_assignments: list[dict]
) -> None:
    """Record GPIO-map rows verbatim, bound to the host refdes.

    Interpretation (which rows can bind, name cross-checks against pinout nets)
    is Stage 4's job — Stage 0 only extracts. Rows are kept raw so a "GPIO— (TBD)"
    row survives to be *counted* later instead of vanishing here.
    """
    source_ref = _make_source_ref(table)
    for row in table.rows:
        gpio = _column_lookup(row, ["gpio"]).strip()
        signal = _column_lookup(row, ["signal"]).strip()
        if not gpio and not signal:
            continue
        entry: dict = {"host": host, "gpio": gpio, "signal": signal, "source_ref": source_ref}
        destination = _column_lookup(row, ["destination", "dest"]).strip()
        notes = _column_lookup(row, ["note"]).strip()
        if destination:
            entry["destination"] = destination
        if notes:
            entry["notes"] = notes
        gpio_assignments.append(entry)


def run(md_files: list[Path], output_path: Path) -> dict:
    """Extract from md_files and write a schema-validated design_artifact.json."""
    artifact = extract([Path(p) for p in md_files])
    schema.validate("design_artifact", artifact)
    schema.dump_json(output_path, artifact)

    # Print what was lost. A warning recorded only in the JSON is barely better
    # than no warning at all — the whole failure mode here is that Stage 0 runs
    # to completion looking successful while quietly leaving pins out of the board.
    for w in artifact.get("warnings", []):
        where = w.get("source_ref") or {}
        loc = where.get("file", "")
        if where.get("line_start"):
            loc = f"{loc}:{where['line_start']}"
        print(f"stage0: warning [{w['code']}] {loc}: {w['summary']}", file=sys.stderr)
        if w.get("fix"):
            print(f"        fix: {w['fix']}", file=sys.stderr)
    return artifact
