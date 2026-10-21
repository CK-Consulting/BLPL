"""The bill of materials as CSV, for people: every part, or only what is bought.

Two different questions get asked of a BOM, and one file answers neither well.
*What is on this board?* wants every footprint, test pads and fiducials
included, with how each is assembled. *What do I buy?* wants only the parts a
supplier ships, grouped by identical part with a quantity, because that is the
list a quote is made from; a bare test pad or a fiducial on it is a line the
supplier has to ask about.

The parts come from the HDM when Stage 5 has written one: it is the board as
built, with the test points Stage 5 synthesises and each part's ``assembly``
from project.yaml. Before Stage 5 only Stage 1's ``bom.json`` exists, which
knows nothing about assembly, so every part in it counts as bought.
"""

from __future__ import annotations

import csv
import io
import re

ALL_COLUMNS = ["Designator", "Value", "MPN", "Manufacturer", "Footprint", "Assembly", "Description"]
PURCHASE_COLUMNS = ["Designators", "Quantity", "Value", "Footprint", "MPN", "Manufacturer"]

#: Assembly states that mean nothing is bought for the part.
NOT_BOUGHT = ("dnp", "footprint_only")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _natural(ref: str) -> tuple:
    # R2 before R10: a spreadsheet's string sort puts R10 first.
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ref))


def parts(bom, hdm) -> list[dict]:
    """One record per part: ref, value, mpn, manufacturer, footprint, assembly,
    description, synthesized. From the HDM when it has components, else bom.json."""
    comps = hdm.get("components") if isinstance(hdm, dict) else None
    out: list[dict] = []
    if isinstance(comps, dict) and comps:
        for ref, c in comps.items():
            if not isinstance(c, dict):
                continue
            out.append({
                "ref": _text(ref),
                "value": _text(c.get("value")),
                "mpn": _text(c.get("mpn")),
                "manufacturer": _text(c.get("manufacturer")),
                "footprint": _text(c.get("footprint")),
                "assembly": _text(c.get("assembly")),
                "description": _text(c.get("description")),
                "synthesized": _text(c.get("synthesized")),
            })
    else:
        if isinstance(bom, dict):
            rows = bom.get("rows") or bom.get("components") or []
        else:
            rows = bom if isinstance(bom, list) else []
        for r in rows:
            if not isinstance(r, dict):
                continue
            out.append({
                "ref": _text(r.get("refdes") or r.get("local_id")),
                "value": _text(r.get("value") or r.get("description")),
                "mpn": _text(r.get("mpn") or r.get("part_hint")),
                "manufacturer": _text(r.get("manufacturer") or r.get("manufacturer_hint")),
                "footprint": _text(r.get("footprint") or r.get("footprint_hint") or r.get("package")),
                "assembly": "",
                "description": _text(r.get("description")),
                "synthesized": "",
            })
    return sorted(out, key=lambda p: _natural(p["ref"]))


def bought(part: dict) -> bool:
    """Whether a supplier ships something for this part.

    Not when project.yaml marks it DNP or footprint-only, not for a test point
    Stage 5 synthesised (a bare pad), and not for a fiducial, which is copper
    and mask on the board itself whether or not project.yaml lists it.
    """
    if part.get("assembly") in NOT_BOUGHT or part.get("synthesized"):
        return False
    ref = part.get("ref", "").upper()
    lib = part.get("footprint", "").split(":", 1)[0].lower()
    return not (ref.startswith("FID") or lib.startswith("fiducial"))


def _cell(value) -> str:
    # Descriptions and part names come from vendors and datasheets. A cell a
    # spreadsheet would read as a formula is opened as text instead.
    s = _text(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(records: list[dict], purpose: str) -> str:
    """``purpose`` "all": one row per part. "purchase": bought parts only,
    grouped by identical part (MPN, value, footprint) with a quantity."""
    buf = io.StringIO()
    if purpose == "all":
        w = csv.writer(buf)
        w.writerow(ALL_COLUMNS)
        for p in records:
            w.writerow([_cell(p["ref"]), _cell(p["value"]), _cell(p["mpn"]), _cell(p["manufacturer"]),
                        _cell(p["footprint"]), p["assembly"] or "fitted", _cell(p["description"])])
        return buf.getvalue()
    if purpose != "purchase":
        raise ValueError(f"purpose must be 'all' or 'purchase', not {purpose!r}")
    grouped: dict[tuple, dict] = {}
    for p in records:
        if not bought(p):
            continue
        entry = grouped.setdefault((p["mpn"], p["value"], p["footprint"]), {
            "refs": [], "value": p["value"], "footprint": p["footprint"],
            "mpn": p["mpn"], "manufacturer": p["manufacturer"]})
        entry["refs"].append(p["ref"])
    w = csv.writer(buf)
    w.writerow(PURCHASE_COLUMNS)
    for e in sorted(grouped.values(), key=lambda e: (e["mpn"], e["value"])):
        refs = sorted(e["refs"], key=_natural)
        w.writerow([",".join(_cell(r) for r in refs), len(refs), _cell(e["value"]), _cell(e["footprint"]),
                    _cell(e["mpn"]), _cell(e["manufacturer"])])
    return buf.getvalue()
