"""Turn a hand-checked pin table into the pinout JSON the pipeline wants.

Deterministic, and that is the point. Everything else in this module's
neighbourhood asks a model to read a datasheet, which is necessary when the
input is a 940-page PDF and wasteful when somebody has already sat down and
produced a clean table. A parser cannot hallucinate a pin, cannot decide that
``analog_in`` is an electrical type, and costs nothing to run again.

Four shapes turn up, and they are told apart by looking rather than by being
declared:

``separated``
    ``(1)  GND  Ground  The pad must be…`` with ``---`` between pins, and
    continuation lines — blank pin number — carrying alternate functions.
    Nordic's module specs read this way.

``repeated``
    One line per function, the pin number repeated for each. The nRF9151's
    table: ``2  P0.20  Digital I/O (SoC)  General purpose I/O.`` followed by
    ``2  AIN7  Analog input  Analog input.``

``flat``
    One line per pin, no alternates. The Wio-LR2021 module.

``cubemx``
    STMicro's CSV export: ``Position,Name,Type,Signal,Label,AF0…AF15``. The best
    input of the four by some distance — the vendor's own tool, keyed by ball
    position, with each alternate function already labelled by peripheral. An
    ``AF7`` cell reading ``USART1_RX`` is a name, a peripheral and an AF code in
    one, which is exactly the shape AltFunction asks for.

What this will not do is guess an electrical type it does not recognise. An
unmapped word becomes ``unspecified`` and is listed in ``notes``, because a pin
silently typed as ``passive`` is a pin that will be wired wrongly and nobody
will know where it came from.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

# The datasheet's word for what a pin is, to the schema's enum, which is
# KiCad's set of electrical types. Longest key first when matching, so
# "digital input" is not swallowed by "digital".
_TYPES: dict[str, str] = {
    "power": "power_in",
    "supply": "power_in",
    "ground": "power_in",
    "gnd": "power_in",
    "power output": "power_out",
    "digital i/o": "bidirectional",
    "digital io": "bidirectional",
    "i/o": "bidirectional",
    "io": "bidirectional",
    "bidirectional": "bidirectional",
    "digital input": "input",
    "digital in": "input",
    "analog input": "input",
    "analog in": "input",
    "nfc input": "input",
    "input": "input",
    "reset": "input",
    "boot": "input",
    "digital output": "output",
    "digital out": "output",
    "trace data": "output",
    "trace clock": "output",
    "output": "output",
    "analog": "passive",
    "rf": "passive",
    "passive": "passive",
    "nc": "not_connected",
    "not connected": "not_connected",
    "-": "passive",
    # Single letters, as the Wio table uses them.
    "i": "input",
    "o": "output",
}

# Rows of dashes, ruled lines, and the note some of these files carry at the top
# explaining their own convention.
# A run of dashes, with or without a word in it. These files end with lines
# like "-----END OF PINOUT---", which is plainly a terminator and was being read
# as a row — giving pin 22 an alternate function called "OF PINOUT-".
_SEPARATOR = re.compile(r"^\s*-{3,}[^|]*?-*\s*$")
# Digits only, optionally parenthesised. Ball positions like ``A1`` exist, but
# only in the CubeMX CSV, which never comes through here — and allowing letters
# made ``XL2`` in the *name* column read as a pin number, so the crystal
# connections on the nRF54L15 became two extra pins numbered "XL1" and "XL2"
# instead of alternate functions of pins 32 and 33. 47 pins where the package
# has 45, and both extras looked plausible enough to survive a glance.
_PIN_NUMBER = re.compile(r"^\s*\(?(?P<num>\d{1,4})\)?\s+(?P<rest>\S.*)$")


class PinmapError(ValueError):
    """The file could not be read as a pin table, with a reason."""


def electrical_type(word: str) -> tuple[str, bool]:
    """(schema type, whether it was recognised).

    Unrecognised is reported, never guessed. A pin quietly typed ``passive``
    because nobody knew what "SWO" meant is a pin that gets wired wrongly with
    no way to trace the decision.
    """
    key = " ".join((word or "").split()).lower().strip(".,")
    if not key:
        return "unspecified", False
    if key in _TYPES:
        return _TYPES[key], True
    # Substring matching, but only for keys long enough to mean something. "i"
    # and "o" are real type words in the Wio table and catastrophic as
    # substrings: "i" is inside "vcc_in", so the first pin of that module was
    # typed `input` and lost its name.
    for k in sorted((k for k in _TYPES if len(k) >= 3), key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", key):
            return _TYPES[k], True
    return "unspecified", False


def _pin(numbers: list[str], name: str, type_word: str, description: str,
         page: int | None, section: str) -> dict[str, Any]:
    kind, known = electrical_type(type_word)
    # A blank cell is not an unmapped word. Saying "unmapped type: ''" told the
    # reader nothing and buried the one row that genuinely had a type nobody
    # recognised.
    notes = None if known or not type_word.strip() else f"unmapped type in source: {type_word!r}"
    out: dict[str, Any] = {
        "numbers": numbers,
        "name": name,
        "type": kind,
        "subtype": None,
        "description": description or None,
        "power_domain": None,
        "alt_functions": [],
        "is_5v_tolerant": None,
        "absolute_max": None,
        "recommended": None,
        "drive_strength": None,
        "notes": notes,
    }
    if page:
        out["evidence"] = {
            "page": page,
            "section": section or None,
            "confidence": "high",
            "method": "table",
        }
    else:
        out["evidence"] = None
    return out


def _columns(line: str) -> list[str]:
    """Split on runs of two or more spaces — the column separator these files
    use. One space is inside a value ("Digital I/O", "Trace clock")."""
    return [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]


def split_row(line: str) -> tuple[str | None, str, str, str]:
    """One line as (pin number, name, type, description).

    Anchored on the *type*, not on column positions. These tables are typed by
    hand and their columns wobble; the set of type words does not. "Ground",
    "Digital I/O", "Analog input" and their friends are a closed vocabulary, so
    finding one tells you where the name ends and the description begins,
    whatever the spacing is doing.

    Reading column positions instead cost two full rewrites. Splitting on
    whitespace alone cannot tell an empty cell from an absent one, so a row
    whose name is blank — Nordic centres the pin number vertically against its
    group, which leaves exactly that — had its type slide left and the pin was
    named "Digital I/O". Detecting boundaries from the data was better until a
    hand-typed row disagreed, at which point a boundary landed inside
    "SPI_MISO" and two pins vanished.

    The vocabulary has no such failure mode: it finds the type or it does not,
    and when it does not the row is reported rather than guessed at.
    """
    tokens = [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
    if not tokens:
        return None, "", "", ""

    number: str | None = None
    m = re.fullmatch(r"\(?(\d{1,4})\)?", tokens[0])
    if m:
        number = m.group(1)
        tokens = tokens[1:]

    # The type never comes before the name, so with a full row the search starts
    # at the second token. Without that, "GND" — a pin name in every datasheet
    # ever written, and also a word in the type vocabulary — was read as its own
    # type, leaving the pin nameless and dropping it.
    #
    # A two-token row is the exception: that is a continuation whose name cell
    # is empty, so its type really is first.
    first = 1 if len(tokens) >= 3 else 0
    where = next(
        (i for i, t in enumerate(tokens) if i >= first and electrical_type(t)[1]),
        None,
    )
    if where is None:
        # No type word: the whole thing is a name, or a name and a note.
        name = tokens[0] if tokens else ""
        return number, name, "", " ".join(tokens[1:])
    name = " ".join(tokens[:where])
    return number, name, tokens[where], " ".join(tokens[where + 1:])


def stated_convention(text: str) -> str | None:
    """The shape the file says it is, read from its own preamble.

    Each of these carries a line near the top explaining how it is meant to be
    read, because every manufacturer lists alternate functions differently:

        "--- indicates the separation line between pins which can have
         multiple functions/description"
        "Each line represents a pin; if a line shares the same pin number as a
         preceding line, the it represents an alterate functoion available on
         that pin"
        "pins do not have noted alternate functions or descripions; each line
         represents a pin"

    That is a statement about the format, and reading it beats inferring one —
    a document with no repeated pin numbers is indistinguishable from a
    ``flat`` one until it turns out the repeats were simply further down.

    Matched on keywords rather than whole phrases, deliberately: these are
    hand-written notes and two of the three above contain typos.
    """
    # Examined line by line, not as one blob. A directive is a *sentence about
    # the format*; looking for its keywords anywhere in the first dozen lines
    # matched a file whose terminator happened to be near the top and read a
    # flat table as a separated one.
    for line in text.splitlines()[:12]:
        low = line.lower()
        if not low.strip():
            continue
        mentions_alt = any(w in low for w in ("alternate", "alterate", "multiple", "function"))
        if "---" in low and ("indicat" in low or "separat" in low) and mentions_alt:
            return "separated"
        if "same pin number" in low and mentions_alt:
            return "repeated"
        if mentions_alt and any(w in low for w in ("do not have", "no noted", "none")):
            return "flat"
    return None


def detect(text: str) -> str:
    """The file's shape: what it says it is, or failing that what it looks like."""
    if text.lstrip().startswith('"Position"') or text.lstrip().startswith("Position,"):
        return "cubemx"
    stated = stated_convention(text)
    if stated:
        return stated
    if any(_SEPARATOR.match(line) for line in text.splitlines()):
        return "separated"
    numbers = [
        m.group("num")
        for line in text.splitlines()
        if (m := _PIN_NUMBER.match(line))
    ]
    if len(numbers) != len(set(numbers)):
        return "repeated"
    return "flat"


def parse(source: str | Path, *, page: int | None = None, section: str = "") -> list[dict]:
    """Read a pin table and return schema-valid pinout JSON."""
    text = Path(source).read_text(encoding="utf-8") if _looks_like_path(source) else str(source)
    shape = detect(text)
    if shape == "cubemx":
        return _parse_cubemx(text, page, section)
    return _parse_columns(text, shape, page, section)


def _looks_like_path(source: str | Path) -> bool:
    if isinstance(source, Path):
        return True
    return "\n" not in source and len(source) < 4096 and Path(source).is_file()


def _data_lines(text: str) -> list[str]:
    """Lines that could be table rows."""
    return [
        line for line in text.splitlines()
        if line.strip() and not _SEPARATOR.match(line) and re.search(r"\S\s{2,}\S", line)
    ]


def _rows(text: str) -> list[tuple[str | None, tuple[str, str, str]] | None]:
    """Every line as (number, (name, type, description)); None marks a block
    boundary."""
    out: list[tuple[str | None, tuple[str, str, str]] | None] = []
    for line in text.splitlines():
        if _SEPARATOR.match(line):
            out.append(None)
            continue
        if not line.strip():
            continue
        number, name, type_word, description = split_row(line)
        if number is None and not name and not type_word:
            continue
        out.append((number, (name, type_word, description)))
    return out


def _is_header(name: str, type_word: str, description: str) -> bool:
    joined = f"{name} {type_word} {description}".lower()
    return "pin name" in joined or ("name" in joined and "description" in joined)


def _parse_columns(text: str, shape: str, page: int | None, section: str) -> list[dict]:
    rows = _rows(text)
    pins: list[dict] = []

    if shape == "separated":
        # A block between separators is one pin. The pin number can sit on any
        # line of it — Nordic centres it vertically against the group — so the
        # block is read whole rather than line by line.
        block: list[tuple[str, str, str]] = []
        number: str | None = None
        for row in rows:
            if row is None:
                _emit_block(pins, number, block, page, section)
                block, number = [], None
                continue
            num, cells = row
            if _is_header(*cells):
                continue
            if num is not None:
                number = num
            block.append(cells)
        _emit_block(pins, number, block, page, section)
        return pins

    for row in rows:
        if row is None:
            continue
        num, (name, type_word, description) = row
        if _is_header(name, type_word, description):
            continue
        if num is None:
            # A continuation belongs to the pin above. Reading it as a new pin
            # is how a pinout gains phantom entries.
            if pins:
                _add_alt(pins[-1], name or description, type_word,
                         "" if not name else description)
            continue
        if shape == "repeated" and pins and num in pins[-1]["numbers"]:
            _add_alt(pins[-1], name or description, type_word,
                     "" if not name else description)
            continue
        pins.append(_pin([num], name, type_word, description, page, section))
    return pins


def _emit_block(pins: list[dict], number: str | None,
                block: list[tuple[str, str, str]], page: int | None, section: str) -> None:
    """One separator-delimited block: the first named row is the pin, the rest
    are its alternate functions."""
    if not number or not block:
        return
    named = [c for c in block if c[0]]
    if not named:
        return
    head = named[0]
    pin = _pin([number], head[0], head[1], head[2], page, section)
    for cells in block:
        if cells is head:
            continue
        name, type_word, description = cells
        if not name:
            # No name but a description: the description *is* the function's
            # name, which is how Nordic writes an alternate —
            #
            #     (2)  P0.04   Digital I/O   General-purpose digital I/O
            #                  Digital I/O   GRTC CLKOUT32K
            #
            # Skipping these lost the alternate on every multi-function pin,
            # silently, while the pin itself looked perfectly extracted.
            if not description:
                continue
            name, description = description, ""
        _add_alt(pin, name, type_word, description)
    pins.append(pin)


def _add_alt(pin: dict, name: str, type_word: str, description: str) -> None:
    if not name:
        return
    pin["alt_functions"].append({
        "name": name,
        "peripheral": _peripheral(name),
        "role": description or None,
        "af_code": None,
    })


def _peripheral(name: str) -> str | None:
    """The peripheral a function name belongs to, when the name says so.

    ``USART1_RX`` names one; ``AIN7`` and ``TRACECLK`` do not, and inventing one
    for them would be worse than leaving it null — which the schema allows
    precisely because plenty of alternate functions have no peripheral.
    """
    m = re.match(r"^([A-Z][A-Z0-9]*\d)_", (name or "").upper())
    return m.group(1) if m else None


def _parse_cubemx(text: str, page: int | None, section: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Position" not in rows[0]:
        raise PinmapError("not a CubeMX pin export: no Position column")
    af_cols = sorted(
        (c for c in rows[0] if re.fullmatch(r"AF\d+", c or "")),
        key=lambda c: int(c[2:]),
    )
    pins: list[dict] = []
    for row in rows:
        position = (row.get("Position") or "").strip()
        name = (row.get("Name") or "").strip()
        if not position or not name:
            continue
        pin = _pin([position], name, (row.get("Type") or "").strip(),
                   (row.get("Label") or "").strip(), page, section)
        signal = (row.get("Signal") or "").strip()
        if signal:
            pin["subtype"] = signal
        for col in af_cols:
            value = (row.get(col) or "").strip()
            if not value:
                continue
            # One cell can hold several, as DCMI_D2/PSSI_D2 does.
            for part in value.split("/"):
                part = part.strip()
                if part:
                    pin["alt_functions"].append({
                        "name": part,
                        "peripheral": _peripheral(part),
                        "role": None,
                        "af_code": col,
                    })
        pins.append(pin)
    return pins


# ---------------------------------------------------------------------------
# Writing it where the pipeline looks
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"


def install(
    pins: list[dict],
    mpn: str,
    cache: Path,
    *,
    pdf: Path | None = None,
    source_file: Path | None = None,
) -> list[Path]:
    """Put a parsed pinout where the extractor would have put it.

    Two files, because two things read them:

    ``<mpn>.pinout.result.json``
        The per-task wrapper. Marked ``complete``, so a later extraction run
        skips the pinout instead of paying a model to redo work somebody has
        already checked by hand.

    ``<mpn>.json``
        The merged record, which is what ``read_datasheet_specs`` serves and
        what the assistant consults when writing a design document. An existing
        file is updated rather than replaced — the other tasks' findings are
        somebody's work too.

    ``model_id`` says ``parsed`` rather than naming a model, because no model
    was involved and a provenance field that implies one is worse than an empty
    one.
    """
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())

    result = {
        "task_id": "pinout",
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "extracted_at": now,
        "model_tier": "none",
        "model_id": f"parsed:{source_file.name}" if source_file else "parsed",
        "data": pins,
    }
    rp = cache / f"{mpn}.pinout.result.json"
    rp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    written.append(rp)

    merged_path = cache / f"{mpn}.json"
    merged: dict[str, Any] = {}
    if merged_path.is_file():
        try:
            merged = json.loads(merged_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            merged = {}
    base = merged.setdefault("base", {})
    base["pinout"] = pins
    package = base.setdefault("package", {})
    package["pin_count"] = len(pins)

    source = merged.setdefault("source", {})
    source.setdefault("mpn", mpn)
    if pdf and pdf.is_file():
        source.setdefault("local_path", str(pdf))
        source.setdefault(
            "sha256",
            "sha256:" + __import__("hashlib").sha256(pdf.read_bytes()).hexdigest(),
        )
    merged.setdefault("schema_version", SCHEMA_VERSION)
    merged.setdefault("categories", [])
    extraction = merged.setdefault("extraction", {})
    extraction["pinout"] = {
        "method": "parsed",
        "source_file": source_file.name if source_file else None,
        "at": now,
        "note": (
            "Read from a hand-checked pin table by blpl.agent.tools.pinmap_parse. "
            "No model was involved: a parser cannot invent a pin."
        ),
    }
    merged_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    written.append(merged_path)
    return written


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _report(path: Path, pins: list[dict]) -> str:
    alts = sum(len(p["alt_functions"]) for p in pins)
    unmapped = sorted({p["notes"] for p in pins if p["notes"]})
    numbers = [n for p in pins for n in p["numbers"]]
    gaps = ""
    if all(n.isdigit() for n in numbers) and numbers:
        seen = {int(n) for n in numbers}
        missing = [n for n in range(1, max(seen) + 1) if n not in seen]
        if missing:
            gaps = f"  MISSING {missing[:10]}"
    line = f"{path.name[:44]:46s} pins={len(pins):4d} alts={alts:5d}{gaps}"
    for note in unmapped:
        line += f"\n{'':48s}{note}"
    return line


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(
        prog="pinmap_parse",
        description="Parse a hand-checked pin table into the pipeline's pinout JSON.",
    )
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="Directory for <name>.pinout.json")
    ap.add_argument("--install", type=Path, metavar="PROJECT",
                    help="Write into PROJECT/datasheets/extracted/ where the "
                         "extractor would have, keyed by MPN.")
    ap.add_argument("--mpn", action="append", default=[], metavar="FILE=MPN",
                    help="Which part a file describes. Repeatable.")
    ap.add_argument("--page", type=int, default=0, help="Datasheet page, for evidence.")
    ap.add_argument("--section", default="", help="Section name, for evidence.")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            files.extend(sorted(list(p.glob("*pinmap*.txt")) + list(p.glob("*pinout*.txt"))
                                + list(p.glob("*pinmap*.csv"))))
        else:
            files.append(p)
    if not files:
        print("no pin tables found", file=__import__("sys").stderr)
        return 2

    mapping = dict(pair.split("=", 1) for pair in args.mpn if "=" in pair)

    for f in files:
        try:
            pins = parse(f, page=args.page or None, section=args.section)
        except (PinmapError, OSError) as exc:
            print(f"{f.name[:44]:46s} FAILED: {exc}")
            continue
        print(_report(f, pins))
        text = f.read_text(encoding="utf-8")
        if detect(text) != "cubemx":
            bad = misaligned_lines(_data_lines(text), expect=4)
            for number, line in bad[:5]:
                print(f"{'':48s}line {number} breaks the column alignment: {line[:60]!r}")
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            dest = args.out / f"{f.stem}.pinout.json"
            dest.write_text(_json.dumps(pins, indent=2), encoding="utf-8")
            print(f"{'':48s}-> {dest}")
        if args.install:
            mpn = mapping.get(f.name)
            if not mpn:
                print(f"{'':48s}no --mpn given for {f.name}; not installed")
                continue
            sheets = Path(args.install) / "datasheets"
            for out in install(pins, mpn, sheets / "extracted",
                               pdf=_matching_pdf(sheets, mpn), source_file=f):
                print(f"{'':48s}-> {out}")
    return 0


def _matching_pdf(sheets: Path, mpn: str) -> Path | None:
    try:
        from blpl.agent.tools import datasheet_files

        found = datasheet_files.resolve(sheets.parent, mpn)
        return found.path if found.ok else None
    except Exception:  # noqa: BLE001 — provenance is a bonus, not a requirement
        return None


if __name__ == "__main__":
    raise SystemExit(main())
