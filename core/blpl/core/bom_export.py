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
import math
import re

ALL_COLUMNS = ["Designator", "Value", "MPN", "Manufacturer", "Footprint", "Assembly", "Description"]
#: Part number first: a quote or a distributor's BOM upload is searched by
#: it. QTY is what the build places; ORDER_QTY adds what assembly loses.
PURCHASE_COLUMNS = ["MPN", "MFG", "QTY", "ATTRITION_%", "ORDER_QTY"]

#: Extra parts to order on top of what a build places, by package class:
#: (percent of the placed quantity, a minimum number of extra parts), the
#: larger of the two. Assembly loses parts to feeder set-up, pick-up misses
#: and rework, and small cut-tape passives most of all. The numbers follow
#: what houses ask for rather than any one standard, because there is none:
#: PCBWay wants 0201/0402 passives and mini-MELFs at least 50 over the
#: placed quantity (and 100 in all), and 1 to 5 spare for ICs, BGAs, QFPs and
#: connectors; JLCPCB adds a per-part attrition of its own at quote time.
#: Where a part needs something else, project.yaml's assembly.attrition sets
#: it by MPN.
ATTRITION = {
    "tiny_passive": (5.0, 50),    # 0201, 0402, mini-MELF: PCBWay's 50 over
    "chip_passive": (3.0, 10),    # 0603, 0805
    "large_passive": (2.0, 5),    # 1206 and up
    "small_discrete": (3.0, 5),   # SOT-23, SOD-323, SC-70 and the like
    "ic": (2.0, 2),               # ICs, modules, connectors: PCBWay's 1 to 5
    "through_hole": (2.0, 1),     # hand- or wave-soldered, rarely lost
    "other": (5.0, 5),            # unclassified: err towards a spare
}

_SIZE = re.compile(r"(?<![0-9])(0201|0402|0603|0805|1008|1206|1210|1812|2010|2220|2512)(?![0-9])")


def package_class(footprint: str) -> str:
    """Which ATTRITION class a footprint falls in, from its library name."""
    f = footprint.lower()
    lib = f.split(":", 1)[0]
    size = _SIZE.search(f.split(":", 1)[-1])
    passive = lib.startswith(("resistor", "capacitor", "inductor", "fuse", "led_smd", "diode_smd"))
    if "minimelf" in f or "mini-melf" in f or (size and size.group(1) in ("0201", "0402")):
        return "tiny_passive"
    if size and passive or size and size.group(1) in ("0603", "0805"):
        return "chip_passive" if size.group(1) in ("0603", "0805") else "large_passive"
    if re.search(r"sot-?\d|sod-?\d|sc-?70|sot-?523|sot-?563|dfn-?\d-1ep_1x", f):
        return "small_discrete"
    if "tht" in f or "throughhole" in f or lib.startswith(("battery", "pinheader", "pinsocket")):
        return "through_hole"
    if re.search(r"qfn|dfn|bga|lga|qfp|soic|ssop|tssop|msop|son|module|xiao|wio|u\.fl|connector|antenna", f):
        return "ic"
    return "other"


def attrition_for(part: dict, overrides: dict | None = None) -> tuple[float, int]:
    """(percent, minimum extra) for one part: project.yaml's by MPN, else its class's."""
    o = (overrides or {}).get(part.get("mpn", ""))
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        return float(o), 0
    if isinstance(o, dict):
        return float(o.get("percent", 0.0)), int(o.get("min_extra", 0))
    return ATTRITION[package_class(part.get("footprint", ""))]


def order_quantity(placed: int, percent: float, min_extra: int) -> int:
    """What to buy so ``placed`` survive assembly: the larger of the two allowances."""
    if placed <= 0:
        return 0
    return placed + max(math.ceil(placed * percent / 100.0 - 1e-9), min_extra)

#: Assembly states that mean nothing is bought for the part.
NOT_BOUGHT = ("dnp", "footprint_only")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _natural(ref: str) -> tuple:
    # R2 before R10: a spreadsheet's string sort puts R10 first.
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ref))


def _bom_rows(bom) -> list[dict]:
    if isinstance(bom, dict):
        rows = bom.get("rows") or bom.get("components") or []
    else:
        rows = bom if isinstance(bom, list) else []
    return [r for r in rows if isinstance(r, dict)]


def _from_bom_row(r: dict, assembly: str = "") -> dict:
    return {
        "ref": _text(r.get("refdes") or r.get("local_id")),
        "value": _text(r.get("value") or r.get("description")),
        "mpn": _text(r.get("mpn") or r.get("part_hint")),
        "manufacturer": _text(r.get("manufacturer") or r.get("manufacturer_hint")),
        "footprint": "" if assembly == "not_placed" else _text(
            r.get("footprint") or r.get("footprint_hint") or r.get("package")),
        "assembly": assembly,
        "description": _text(r.get("description")),
        "synthesized": "",
    }


def parts(bom, hdm) -> list[dict]:
    """One record per part: ref, value, mpn, manufacturer, footprint, assembly,
    description, synthesized. From the HDM when it has components, else bom.json.

    A part Stage 5 kept off the board (``hdm.not_placed``: a coin cell in a
    holder elsewhere, a cable) is not among the HDM's components but is still
    bought, so it comes back from bom.json marked ``not_placed``. Taking the
    HDM's components as the whole list dropped it from the purchase export
    once Stage 5 had run (Codex, PR #70).
    """
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
        off_board = {_text(r) for r in (hdm.get("not_placed") or [])}
        out.extend(_from_bom_row(r, "not_placed") for r in _bom_rows(bom)
                   if _text(r.get("refdes") or r.get("local_id")) in off_board
                   and _text(r.get("refdes") or r.get("local_id")) not in comps)
    else:
        out.extend(_from_bom_row(r) for r in _bom_rows(bom))
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


def to_csv(records: list[dict], purpose: str, *, boards: int = 1,
           attrition: dict | None = None) -> str:
    """``purpose`` "all": one row per part. "purchase": bought parts only, one
    line per part number and maker, for a build of ``boards``: MPN, MFG, the
    quantity placed, the attrition allowed for and the quantity to order.
    ``attrition`` is project.yaml's assembly.attrition, by MPN."""
    if boards < 1:
        raise ValueError(f"boards must be at least 1, not {boards}")
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
    # One line per part number and maker. A part with no MPN still gets a line,
    # grouped by value and footprint, with the gap said in the MPN cell: left
    # out, it would be missing from the quote and nobody would notice.
    grouped: dict[tuple, dict] = {}
    for p in records:
        if not bought(p):
            continue
        if p["mpn"]:
            key = ("mpn", p["mpn"].upper(), p["manufacturer"].upper())
        else:
            key = ("none", p["value"], p["footprint"])
        entry = grouped.setdefault(key, {"mpn": p["mpn"], "mfg": p["manufacturer"], "refs": [],
                                         "value": p["value"], "footprint": p["footprint"]})
        entry["refs"].append(p["ref"])
    w = csv.writer(buf)
    w.writerow(PURCHASE_COLUMNS)
    for key, e in sorted(grouped.items(), key=lambda kv: (kv[0][0] == "none", kv[1]["mpn"].upper(), kv[1]["value"])):
        refs = sorted(e["refs"], key=_natural)
        mpn = e["mpn"] or "NO MPN: " + " ".join(x for x in (e["value"], e["footprint"], f"({','.join(refs)})") if x)
        placed = len(refs) * boards
        percent, min_extra = attrition_for({"mpn": e["mpn"], "footprint": e["footprint"]}, attrition)
        w.writerow([_cell(mpn), _cell(e["mfg"]), placed, f"{percent:g}",
                    order_quantity(placed, percent, min_extra)])
    return buf.getvalue()
