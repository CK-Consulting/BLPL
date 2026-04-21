"""Deterministic Markdown pipe-table extractor with source-line tracking.

Pure stdlib (no pandas). Used by Stage 0 to convert BOM/pinout Markdown tables
into structured rows that are then assembled into a design_artifact.v1 document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|\s*$")


@dataclass(frozen=True)
class ParsedTable:
    headers: list[str]
    rows: list[dict[str, str]]
    source_file: str
    line_start: int  # 1-based, line of the header row
    line_end: int    # 1-based, line of the last data row


def _clean_cell(cell: str) -> str:
    return cell.strip().strip("*").strip()


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_clean_cell(c) for c in stripped.split("|")]


def extract_tables(text: str, source_file: str) -> list[ParsedTable]:
    """Extract all pipe-delimited Markdown tables with 1-based line refs."""
    lines = text.splitlines()
    tables: list[ParsedTable] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("|") and i + 1 < len(lines) and _SEPARATOR_RE.match(lines[i + 1].strip()):
            headers = _split_row(line)
            header_line = i + 1  # 1-based
            rows: list[dict[str, str]] = []
            j = i + 2
            last_data_line = header_line
            while j < len(lines):
                row_stripped = lines[j].strip()
                if not row_stripped.startswith("|"):
                    break
                cells = _split_row(lines[j])
                # Pad or truncate cells to header length.
                if len(cells) < len(headers):
                    cells = cells + [""] * (len(headers) - len(cells))
                elif len(cells) > len(headers):
                    cells = cells[: len(headers)]
                rows.append(dict(zip(headers, cells, strict=True)))
                last_data_line = j + 1  # 1-based
                j += 1
            if rows:
                tables.append(
                    ParsedTable(
                        headers=headers,
                        rows=rows,
                        source_file=source_file,
                        line_start=header_line,
                        line_end=last_data_line,
                    )
                )
            i = j
            continue
        i += 1
    return tables


def extract_tables_from_file(path: Path) -> list[ParsedTable]:
    text = Path(path).read_text(encoding="utf-8")
    # source_file is recorded relative to cwd if under it, else absolute.
    try:
        rel = Path(path).resolve().relative_to(Path.cwd())
        source_file = str(rel)
    except ValueError:
        source_file = str(Path(path).resolve())
    return extract_tables(text, source_file)


_BOM_HEADER_TOKENS = {"ref", "reference", "part number", "part", "description", "package", "mpn"}
_PINOUT_HEADER_TOKENS_PIN = {"pin", "ball", "pad"}
_PINOUT_HEADER_TOKENS_SIGNAL = {"signal", "signal name", "function"}


def classify(table: ParsedTable) -> str:
    """Return 'bom', 'pinout', or 'other' for a parsed table.

    Relaxed matching: we check whether header cells contain known tokens (case
    insensitive) rather than requiring exact column names.
    """
    header_lower = [h.lower() for h in table.headers]
    has_pin = any(any(tok in h for tok in _PINOUT_HEADER_TOKENS_PIN) for h in header_lower)
    has_signal = any(any(tok in h for tok in _PINOUT_HEADER_TOKENS_SIGNAL) for h in header_lower)
    if has_pin and has_signal:
        return "pinout"
    bom_hits = sum(1 for h in header_lower if any(tok == h or tok in h for tok in _BOM_HEADER_TOKENS))
    if bom_hits >= 2:
        return "bom"
    return "other"
