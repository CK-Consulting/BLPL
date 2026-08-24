"""Turning an extracted pin map into the markdown Stage 0 actually reads.

Stage 0, doctor and every stage after them are deterministic scripts. They
cannot infer, ask, or be persuaded — a table either has the columns they look
for or it is dropped, and a pin range is not a pin. That puts the whole burden
of matching the format on whoever writes the document, which for this project is
usually a model, and a model asked to hand-write 216 rows of BGA pinout will
spend a fortune getting some of them wrong.

It does not have to. The pin maps are already extracted, verified against the
datasheet, and sitting in ``datasheets/extracted/``. This renders one into the
exact shape Stage 0 parses, so the model's job becomes citing a pin map rather
than reproducing one.

The two rules that matter, both learned from a doctor run rather than guessed:

- **One row per pin.** An extracted entry carries ``numbers`` — a list, because
  one signal often lands on several balls — and a row per number is what Stage 0
  maps one-to-one. Writing ``81–104`` in a Pin cell is what produced DOC-003.
- **The heading anchors the table**, and only a refdes beginning ``U`` or ``J``
  is recognised there. A pinout under ``## SPKR1`` binds to nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HEADER = "| Pin | Signal | Function |"
RULE = "|---|---|---|"

_NUM = re.compile(r"(\d+)")


def _sort_key(pin: str) -> tuple:
    """Order pins the way a datasheet does: A1, A2, A10, B1 — not A1, A10, A2.

    Split into text and number runs so the numbers compare as numbers. A BGA
    ball and a QFN pin both fall out of this correctly, which is the only reason
    it is worth more than `sorted()`.
    """
    parts = _NUM.split(pin.strip())
    return tuple((int(p) if p.isdigit() else p.lower()) for p in parts if p != "")


def rows(data: list[dict]) -> list[dict]:
    """One row per pin number, in datasheet order."""
    out: list[dict] = []
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        # `function` is what Stage 0 calls the third column; the extraction calls
        # the same thing `description`, falling back to the pin's electrical type
        # when the datasheet gave no prose.
        function = (entry.get("description") or entry.get("type") or "").strip()
        for number in entry.get("numbers") or []:
            pin = str(number).strip()
            if pin:
                out.append({"pin": pin, "signal": name, "function": function})
    out.sort(key=lambda r: _sort_key(r["pin"]))
    return out


def render(refdes: str, mpn: str, data: list[dict], *, source: str = "") -> str:
    """A `## <refdes> — pinout` section, ready to paste into a design document."""
    pins = rows(data)
    cite = f" — from `{source}`" if source else ""
    lines = [
        f"## {refdes} — pinout",
        "",
        f"{mpn}, {len(pins)} pins{cite}. Generated from the extracted pin map; "
        "regenerate rather than edit by hand.",
        "",
        HEADER,
        RULE,
    ]
    for r in pins:
        # A pipe inside a cell would end it early and shift every column after.
        cells = [r["pin"], r["signal"], r["function"]]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def extracted_path(project_dir: Path, mpn: str) -> Path | None:
    """The extracted pin map for this MPN, if one has been produced.

    Two things this must not do, both found in review.

    It must not leave the folder. An MPN is a string from a design document, and
    ``d / f"{mpn}.pinout.result.json"`` with ``../../../`` in it resolved to
    another project's extraction and loaded it — the existence check was "is
    there a file there", which is not the same question as "is it ours". Every
    candidate is now resolved and required to sit inside this project's
    ``datasheets/extracted``.

    And it must not answer for a part that was not asked for. Matching on a
    shared prefix meant that with ``BD0926-V9.3`` cached, asking for
    ``BD0926-V9.32`` — a different orderable part — returned the first one's pin
    map. Orderable variants routinely share a prefix and differ in package or
    pinout, so a near miss here is a plausible, wrong board. The name must match
    exactly once punctuation and case are set aside; anything else returns None
    and the caller is told to extract.
    """
    d = (Path(project_dir) / "datasheets" / "extracted").resolve()
    if not d.is_dir():
        return None

    def inside(p: Path) -> Path | None:
        try:
            r = p.resolve()
        except OSError:
            return None
        return r if r.is_file() and r.is_relative_to(d) else None

    exact = inside(d / f"{mpn}.pinout.result.json")
    if exact:
        return exact
    # The datasheet may spell the part differently from the BOM — case, dashes,
    # dots — but it is the same string underneath or it is a different part.
    flat = re.sub(r"[^A-Za-z0-9]", "", mpn).upper()
    if not flat:
        return None
    for f in sorted(d.glob("*.pinout.result.json")):
        stem = re.sub(r"[^A-Za-z0-9]", "", f.name[: -len(".pinout.result.json")]).upper()
        if stem == flat:
            return inside(f)
    return None


def load(path: Path) -> list[dict]:
    """The pin list out of an extraction result."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else payload
    return data if isinstance(data, list) else []
