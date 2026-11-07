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
    # The escape comes off here, at the end, so the value is what was written.
    return cell.strip().strip("*").strip().replace("\\|", "|")


# A pipe that is part of a value rather than a cell boundary, written the way
# every Markdown renderer expects. Splitting on it as a delimiter turned a
# signal named `A|B` into a cell `A\\` and shifted every column after it along —
# silently, because the row still had the shape of a row.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|") :
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [_clean_cell(c) for c in _UNESCAPED_PIPE.split(stripped)]


# A fenced code block, as CommonMark has it: up to three spaces of indent, then
# three or more backticks or tildes. It closes on a line of the same character,
# at least as long, with nothing after it; one never closed runs to the end.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def fenced_lines(lines: list[str]) -> list[bool]:
    """For each line, whether it is part of a fenced code block, fences included.

    A table or a heading shown as an example inside a fence is text, not part
    of the design. Reading it anyway made a decision log's quoted pin table a
    pinout of nothing, and a ``# comment`` in a shell snippet the heading that
    anchored the table below it, so wrapping something in a fence, the one way
    Markdown has to say "this is not the document", changed nothing.
    """
    out: list[bool] = []
    fence: str | None = None
    for line in lines:
        m = _FENCE_RE.match(line)
        if fence is None:
            # A backtick fence's info string may not itself hold a backtick.
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                out.append(True)
                continue
            out.append(False)
        else:
            out.append(True)
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not m.group(2).strip()
            ):
                fence = None
    return out


def heading_above(text: str, line_start: int) -> tuple[str | None, int | None]:
    """The nearest heading at or above a 1-based line, and its line number.

    Lines inside a fenced code block are skipped: a ``#`` there is a comment.
    """
    lines = text.splitlines()
    fenced = fenced_lines(lines)
    for i in range(min(line_start, len(lines)) - 1, -1, -1):
        if not fenced[i] and lines[i].lstrip().startswith("#"):
            return lines[i], i + 1
    return None, None


def extract_tables(text: str, source_file: str) -> list[ParsedTable]:
    """Extract all pipe-delimited Markdown tables with 1-based line refs.

    Tables inside fenced code blocks are not tables; see ``fenced_lines``.
    """
    lines = text.splitlines()
    fenced = fenced_lines(lines)
    tables: list[ParsedTable] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if (
            not fenced[i]
            and stripped.startswith("|")
            and i + 1 < len(lines)
            and not fenced[i + 1]
            and _SEPARATOR_RE.match(lines[i + 1].strip())
        ):
            headers = _split_row(line)
            header_line = i + 1  # 1-based
            rows: list[dict[str, str]] = []
            j = i + 2
            last_data_line = header_line
            while j < len(lines):
                row_stripped = lines[j].strip()
                if fenced[j] or not row_stripped.startswith("|"):
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


_REF_HEADERS = {"ref", "refdes", "reference"}
_PIN_N_HEADER = re.compile(r"^pin\s*(\d+)(?:\s*net)?$")


def is_connections(table: ParsedTable) -> bool:
    """A table that says which net each pin of a named part is on.

    Two shapes, both keyed by a ``Ref`` column, because a pinout table is
    anchored by its heading and so can only describe one part: a resistor,
    a capacitor and a ferrite bead each needing a ``## R8 — pinout`` section
    of their own is why no passive in a real design had any connection.

        | Ref | Pin | Net |            one row per pin
        | Ref | Pin 1 | Pin 2 | ... |  one row per part, a column per pin
    """
    header_lower = [h.lower().strip() for h in table.headers]
    if not any(h in _REF_HEADERS for h in header_lower):
        return False
    if "net" in header_lower and "pin" in header_lower:
        return True
    return sum(1 for h in header_lower if _PIN_N_HEADER.match(h)) >= 2


def classify(table: ParsedTable) -> str:
    """Return 'connections', 'bom', 'pinout', 'gpio', or 'other' for a parsed table.

    Relaxed matching: we check whether header cells contain known tokens (case
    insensitive) rather than requiring exact column names.

    'gpio' is a GPIO-assignment map (GPIO | Signal | Destination | ...): which
    host pin drives which subsystem signal. It is deliberately checked *after*
    pinout — a table with both a pin-ish and a signal-ish column is a pinout
    even if a header happens to mention GPIO.
    """
    header_lower = [h.lower() for h in table.headers]
    if is_connections(table):
        return "connections"
    has_pin = any(any(tok in h for tok in _PINOUT_HEADER_TOKENS_PIN) for h in header_lower)
    has_signal = any(any(tok in h for tok in _PINOUT_HEADER_TOKENS_SIGNAL) for h in header_lower)
    if has_pin and has_signal:
        return "pinout"
    if has_signal and any("gpio" in h for h in header_lower):
        return "gpio"
    bom_hits = sum(1 for h in header_lower if any(tok == h or tok in h for tok in _BOM_HEADER_TOKENS))
    if bom_hits >= 2:
        return "bom"
    return "other"
