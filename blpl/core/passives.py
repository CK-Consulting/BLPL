"""When two passives are the same part to order, and when they only look it.

Grouping a BOM by manufacturer part number is always safe and often wasteful: a
10k 0402 from Yageo and the identical part from KOA are two lines nobody needed
to keep apart. Grouping by *value* is what procurement actually wants — one line
saying 47 × 100nF 0402 — and it is the grouping that can hurt someone.

The failure is specific. A 100nF X7R decoupling capacitor and a 100nF Y2-rated
safety capacitor are the same nominal value and are not remotely the same part;
one sits across mains isolation and the other cannot legally go there. Same for
a 10k at 1/16 W and a 10k at 1/4 W where the second was chosen because the first
burns. Merge either pair and the order is wrong in a way the BOM will not show,
because after the merge there is nothing left that says they differed.

So this module is built around one rule, stated as plainly as it can be:

    **Absence of information is never equivalence.**

If two parts cannot be *shown* to match on every attribute that could matter,
they stay separate. Not knowing a capacitor's dielectric is a reason to keep it
apart, not a reason to assume it is ordinary. That makes the grouping more
conservative than a person would be — which is correct, because a person can see
the split and merge it deliberately, and cannot see a merge that should not have
happened.

**Inferred is not known.** The BOM schema carries these attributes now, but most
rows will not fill them in for a long while, so anything absent is read out of
free text instead. A value parsed from a description is a hint: good enough to
*offer* a grouping for a human to confirm, never good enough to order from
unreviewed. Anything inferred is marked as such and carried through to the
output, so the distinction survives to the point where somebody acts on it.

**No-substitutions bars grouping entirely.** A line marked that way is never
merged with an equivalent part however completely both are specified — "you may
order either of these" is precisely the statement the flag exists to withhold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Attributes whose difference is never coincidental, and whose absence therefore
# blocks a merge. Everything here changes what the part *is for* rather than
# who made it.
#
# safety_class is the sharpest: an X- or Y-class capacitor is qualified to sit
# across mains, and an unrated one of the same value is not. Merging them is a
# safety failure, not a procurement inefficiency.
# Attributes that must *match*, blank included. safety_class is the sharpest: an
# X- or Y-class capacitor may sit across mains and an unrated one of the same
# value may not, so a rated part never merges with an unrated one. Two parts that
# are both blank do merge — otherwise nothing groups, ever — but a line resting
# on that gets flagged, because it is an assumption rather than a fact.
MUST_MATCH = ("safety_class", "special")

# Weakest first. An order line is only as good as the least-supported attribute
# behind it, so a line mixing a vendor-confirmed part with a regex-guessed one
# reports itself as guessed.
PROVENANCE_RANK = ("inferred", "unknown", "design_document", "manual", "vendor")


def weakest_provenance(values) -> str:
    ranked = [v for v in values if v in PROVENANCE_RANK]
    if not ranked:
        return "unknown"
    return min(ranked, key=PROVENANCE_RANK.index)

# Attributes that must be *known* before two parts can be shown equivalent, per
# component class. Requiring a power rating on a capacitor or a dielectric on a
# resistor is not caution, it is a bug: it makes every line ungroupable and the
# feature useless, which is its own way of being unsafe — a view nobody can use
# gets replaced by a spreadsheet nobody checks.
REQUIRED_BY_CLASS: dict[str, tuple[str, ...]] = {
    "C": ("voltage_v", "dielectric", "tolerance"),
    "R": ("power_w", "tolerance"),
    "RN": ("power_w", "tolerance"),
    "L": ("tolerance",),
    "FB": (),
    "D": (),
    "LED": (),
}
# Anything not listed: value and package only. An unknown class is not an
# invitation to invent requirements for it.
_DEFAULT_REQUIRED: tuple[str, ...] = ()


def required_attributes(component_class: str) -> tuple[str, ...]:
    return REQUIRED_BY_CLASS.get((component_class or "").upper(), _DEFAULT_REQUIRED)

_SI = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0,
}

# "10k", "4k7", "100nF", "2.2uH", "0R022" — the forms people actually write,
# including the European decimal-in-the-multiplier convention.
_VALUE_RE = re.compile(
    r"\b(?P<whole>\d+)(?P<mult>[pnuµmkKMGRr])(?P<frac>\d*)(?P<unit>[FHΩ]?)\b"
    r"|\b(?P<plain>\d+(?:\.\d+)?)\s*(?P<pmult>[pnuµmkKMG]?)(?P<punit>[FHΩ]|ohm|ohms)\b",
    re.IGNORECASE,
)

_TOLERANCE_RE = re.compile(r"±?\s*(\d+(?:\.\d+)?)\s*%")
_VOLTAGE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*V(?:DC|AC)?\b", re.IGNORECASE)
# 1/4W and 0.25W both appear constantly; so does "250mW".
_POWER_RE = re.compile(
    r"\b(?:(\d+)\s*/\s*(\d+)|(\d+(?:\.\d+)?))\s*(m?)W\b", re.IGNORECASE
)
_DIELECTRIC_RE = re.compile(r"\b(C0G|NP0|X5R|X7R|X7S|X8R|Y5V|Z5U)\b", re.IGNORECASE)
_SAFETY_RE = re.compile(r"\b([XY][12])\b")

# Words that mean "this one was chosen on purpose and is not the ordinary part".
# Their presence blocks a merge with anything that lacks them.
_SPECIAL_TERMS = (
    "fusible", "flameproof", "flame-proof", "anti-surge", "antisurge", "pulse",
    "safety", "mains", "automotive", "aec-q200", "aec-q", "high-voltage",
    "hv", "precision", "current-sense", "current sense", "shunt", "non-inductive",
)


@dataclass(frozen=True)
class PassiveSpec:
    """What a passive is, in the terms that decide whether two are one order."""

    value: float | None = None            # base SI units: ohms, farads, henries
    value_text: str = ""                  # as written, for display
    unit: str = ""                        # "Ω" | "F" | "H"
    tolerance: float | None = None        # percent
    voltage_v: float | None = None
    power_w: float | None = None
    dielectric: str = ""
    safety_class: str = ""                # X1 | X2 | Y1 | Y2
    special: tuple[str, ...] = ()         # fusible, flameproof, AEC-Q200, …
    package: str = ""
    # This exact part, no alternate, unless engineering and design agree. It
    # bars equivalence grouping outright rather than participating in it: the
    # whole meaning of an equivalence line is "order either of these", which is
    # the one thing this flag exists to forbid.
    no_substitutions: bool = False
    no_substitutions_reason: str = ""
    # How much the attributes above can be leaned on. Ranked in PROVENANCE_RANK;
    # a grouping is only as trustworthy as its weakest member, so this has to
    # travel with the part rather than being recomputed at the end.
    provenance: str = "unknown"
    # Which fields were read out of free text rather than declared. A hint is
    # not a fact, and the difference has to survive to whoever orders the parts.
    inferred: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_inferred(self) -> bool:
        return bool(self.inferred)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "value_text": self.value_text,
            "unit": self.unit,
            "tolerance": self.tolerance,
            "voltage_v": self.voltage_v,
            "power_w": self.power_w,
            "dielectric": self.dielectric,
            "safety_class": self.safety_class,
            "special": list(self.special),
            "package": self.package,
            "no_substitutions": self.no_substitutions,
            "no_substitutions_reason": self.no_substitutions_reason,
            "provenance": self.provenance,
            "inferred": sorted(self.inferred),
        }


def _parse_value(text: str) -> tuple[float | None, str, str]:
    m = _VALUE_RE.search(text or "")
    if not m:
        return None, "", ""
    if m.group("whole") is not None:
        whole, mult, frac = m.group("whole"), m.group("mult"), m.group("frac") or ""
        # "4k7" is 4.7k — the multiplier stands in for the decimal point.
        number = float(f"{whole}.{frac}") if frac else float(whole)
        scale = _SI.get(mult, 1.0)
        unit = (m.group("unit") or "").upper()
        return number * scale, m.group(0), ("Ω" if unit in ("", "Ω") else unit)
    number = float(m.group("plain"))
    scale = _SI.get(m.group("pmult") or "", 1.0)
    raw_unit = (m.group("punit") or "").upper()
    unit = "Ω" if raw_unit.startswith("OHM") else raw_unit
    return number * scale, m.group(0), unit


def _parse_power(text: str) -> float | None:
    m = _POWER_RE.search(text or "")
    if not m:
        return None
    if m.group(1) and m.group(2):
        watts = float(m.group(1)) / float(m.group(2))
    else:
        watts = float(m.group(3))
    return watts / 1000 if m.group(4) else watts


def extract(row: dict) -> PassiveSpec:
    """Read a passive's attributes, preferring declared fields over free text.

    Declared fields win outright. Everything taken from ``description`` is
    recorded in ``inferred``, because a grouping built on parsed prose has to
    announce itself as provisional wherever it surfaces.
    """
    # Free text is the fallback source, and only the fallback: `value` is not in
    # here, because reading the field a person filled in is not inference.
    text = " ".join(str(row.get(k) or "") for k in ("description", "notes")).strip()
    inferred: set[str] = set()

    def declared(key: str):
        v = row.get(key)
        return v if v not in (None, "") else None

    # A declared value still has to be parsed — the schema holds it as written
    # ("10k", "100nF") because that is what an engineer types and what a
    # silkscreen shows. Parsing a declared field is reading it, not guessing it;
    # the same regex over a description is a guess. Same code, different
    # provenance, and the difference is the whole point of the distinction.
    value, value_text, unit = None, "", ""
    raw_value = declared("value")
    if raw_value is not None:
        if isinstance(raw_value, (int, float)):
            value, value_text, unit = float(raw_value), str(raw_value), ""
        else:
            value, value_text, unit = _parse_value(str(raw_value))
            if value is None:
                inferred.add("value")     # the field was there and unreadable
    if value is None:
        value, value_text, unit = _parse_value(text)
        if value is not None:
            inferred.add("value")

    tolerance = declared("tolerance")
    if tolerance is None:
        m = _TOLERANCE_RE.search(text)
        if m:
            tolerance = float(m.group(1))
            inferred.add("tolerance")

    voltage = declared("voltage_v")
    if voltage is None:
        m = _VOLTAGE_RE.search(text)
        if m:
            voltage = float(m.group(1))
            inferred.add("voltage_v")

    power = declared("power_w")
    if power is None:
        power = _parse_power(text)
        if power is not None:
            inferred.add("power_w")

    dielectric = declared("dielectric") or ""
    if not dielectric:
        m = _DIELECTRIC_RE.search(text)
        if m:
            dielectric = m.group(1).upper()
            inferred.add("dielectric")

    safety = declared("safety_class") or ""
    if not safety:
        m = _SAFETY_RE.search(text)
        if m:
            safety = m.group(1).upper()
            inferred.add("safety_class")

    low = text.lower()
    special = tuple(sorted({t for t in _SPECIAL_TERMS if t in low}))
    if special:
        inferred.add("special")

    return PassiveSpec(
        value=value,
        value_text=value_text,
        unit=unit,
        tolerance=float(tolerance) if tolerance is not None else None,
        voltage_v=float(voltage) if voltage is not None else None,
        power_w=float(power) if power is not None else None,
        dielectric=dielectric,
        safety_class=safety,
        special=special,
        package=str(row.get("package") or ""),
        no_substitutions=bool(row.get("no_substitutions")),
        no_substitutions_reason=str(row.get("no_substitutions_reason") or ""),
        # Anything read out of prose here is inferred whatever the row claims —
        # a row cannot vouch for a field it did not actually carry.
        provenance=(
            "inferred"
            if inferred
            else str(row.get("attribute_provenance") or "unknown")
        ),
        inferred=frozenset(inferred),
    )


def may_merge(
    a: PassiveSpec, b: PassiveSpec, component_class: str = ""
) -> tuple[bool, str]:
    """Whether two passives can be ordered as one line, and why not if not.

    Conservative on purpose. A split a person can see, they can merge
    deliberately; a merge that should not have happened leaves nothing behind
    to notice.
    """
    for side in (a, b):
        if side.no_substitutions:
            why = side.no_substitutions_reason or "no reason recorded"
            return False, (
                f"one of them is marked no-substitutions ({why}). Grouping it with an "
                "equivalent part is exactly the substitution that needs engineering "
                "and design agreement first."
            )
    if a.value is None or b.value is None:
        return False, "one of them has no value that could be read"
    if a.unit != b.unit:
        return False, f"different units ({a.unit or '?'} vs {b.unit or '?'})"
    # Floats from parsed text: compare relatively rather than exactly.
    if a.value and abs(a.value - b.value) / max(abs(a.value), abs(b.value)) > 1e-9:
        return False, f"different values ({a.value_text or a.value} vs {b.value_text or b.value})"
    if a.package.strip().lower() != b.package.strip().lower():
        return False, f"different packages ({a.package or '?'} vs {b.package or '?'})"

    for attr in required_attributes(component_class):
        av, bv = getattr(a, attr), getattr(b, attr)
        if av is None or bv is None or av == "" or bv == "":
            return False, (
                f"{attr.replace('_', ' ')} is not recorded on both, so they cannot be "
                "shown to be the same part"
            )
        if av != bv:
            return False, f"different {attr.replace('_', ' ')} ({av} vs {bv})"

    # The rule the module exists for. A rated part never merges with an unrated
    # one: a 100nF with no safety class recorded may or may not be the Y2, and
    # "may" is not good enough to put on a purchase order.
    if (a.safety_class or "") != (b.safety_class or ""):
        av, bv = a.safety_class or "none recorded", b.safety_class or "none recorded"
        return False, f"different safety class ({av} vs {bv})"

    if set(a.special) != set(b.special):
        only = set(a.special) ^ set(b.special)
        return False, (
            f"one is specified as {', '.join(sorted(only))} and the other is not"
        )
    return True, ""


def equivalence_key(spec: PassiveSpec, component_class: str = "") -> str | None:
    """A key two orderable-together passives share, or None if it cannot be built.

    None whenever anything that could matter is unknown — which is the same rule
    as may_merge, expressed so a dict can do the grouping.
    """
    # Never groupable, however completely it is specified. A perfectly
    # characterised part that must not be substituted still must not be
    # substituted.
    if spec.no_substitutions:
        return None
    if spec.value is None or not spec.package:
        return None
    parts = [f"{spec.value:.12g}{spec.unit}", spec.package.strip().lower()]
    for attr in required_attributes(component_class):
        v = getattr(spec, attr)
        if v is None or v == "":
            return None
        parts.append(f"{attr}={v}")
    # Blank is a value here, and matches only blank.
    parts.append(f"safety={spec.safety_class.upper() or 'none'}")
    parts.append("special=" + (",".join(sorted(spec.special)) or "none"))
    return "|".join(parts)


def why_unmergeable(spec: PassiveSpec, component_class: str = "") -> str:
    """What is missing before this part could be grouped by value."""
    if spec.no_substitutions:
        return (
            "marked no-substitutions"
            + (f" ({spec.no_substitutions_reason})" if spec.no_substitutions_reason else "")
        )
    if spec.value is None:
        return "no value could be read from it"
    if not spec.package:
        return "no package"
    missing = [
        a.replace("_", " ")
        for a in required_attributes(component_class)
        if getattr(spec, a) in (None, "")
    ]
    if missing:
        return f"nothing recorded for {', '.join(missing)}"
    return ""
