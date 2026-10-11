"""One BOM for a whole configuration, without losing which board a part is on.

A per-board BOM is what the board is built from. A project BOM is what someone
orders, and it is read by different people asking different questions —
procurement wants a line per part number with a total, engineering wants to know
which board a failing part sits on, and support wants both at once.

So aggregation keeps two views of the same rows rather than choosing:

*Grouped* answers "what do I buy": one line per manufacturer part number, with a
quantity and the boards it appears on. *Qualified* answers "where is it": every
original row, prefixed with its board's designator, so ``1.2r19.R26`` and
``1.2r19.2r5.C49`` place a part exactly.

The prefix only exists here. On the board a part is ``R26``, because that is what
fits on a silkscreen and what someone holding it reads; a board's own BOM is
never rewritten to carry a project-level name it has no use for.

**Friendly names are optional and never invented.** A row without one is an
``info``, not a warning — nobody's build is blocked because a capacitor lacks a
nickname. What the tool does instead is *suggest*, and note the gap somewhere a
commit hook can pick it up, so the question gets asked by a person at the moment
they are already thinking about the part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from . import passives
from .designators import ComponentRef, Designator, qualify
from .project_manifest import Configuration, ProjectManifest


@dataclass
class QualifiedRow:
    """One part, on one board, with enough prefix to say which."""

    ref: ComponentRef
    board: str
    mpn: str
    manufacturer: str = ""
    package: str = ""
    description: str = ""
    friendly_name: str = ""
    spec: passives.PassiveSpec | None = None

    @property
    def designator(self) -> str:
        return str(self.ref)

    @property
    def local(self) -> str:
        """What is printed on the board."""
        return self.ref.local

    def to_dict(self) -> dict:
        return {
            "designator": self.designator,
            "local": self.local,
            "board": self.board,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "package": self.package,
            "description": self.description,
            "friendly_name": self.friendly_name,
            "spec": self.spec.to_dict() if self.spec else None,
        }


@dataclass
class EquivalenceLine:
    """Parts from different vendors that could be ordered as one line.

    Advisory, always. This is the view procurement wants and the view that can
    do harm, so it is offered for confirmation rather than presented as the
    order — and anything that cannot be *shown* equivalent is left out of it
    entirely rather than being quietly folded in.
    """

    key: str
    value_text: str
    package: str
    quantity: int = 0
    mpns: list[str] = field(default_factory=list)
    designators: list[str] = field(default_factory=list)
    inferred_from_text: bool = False
    provenance: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            # Weakest member wins: a line mixing a vendor-confirmed part with a
            # regex-guessed one is a guessed line, and saying otherwise would
            # launder the guess.
            "provenance": self.provenance,
            "value": self.value_text,
            "package": self.package,
            "quantity": self.quantity,
            "mpns": list(self.mpns),
            "designators": list(self.designators),
            # True when any attribute behind this grouping was read out of prose
            # rather than declared. The line still needs a human before it is
            # ordered from, and this is how it says so.
            "inferred_from_text": self.inferred_from_text,
        }


@dataclass
class GroupedRow:
    """One part number, and everywhere it is used."""

    mpn: str
    manufacturer: str = ""
    package: str = ""
    description: str = ""
    friendly_name: str = ""
    designators: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.designators)

    def to_dict(self) -> dict:
        return {
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "package": self.package,
            "description": self.description,
            "friendly_name": self.friendly_name,
            "quantity": self.quantity,
            "boards": list(self.boards),
            # Every place this part number is used, so a failure in the field
            # can be traced to a board without opening anything else.
            "designators": list(self.designators),
        }


@dataclass
class AggregateBom:
    project_id: str
    configuration: str
    grouped: list[GroupedRow] = field(default_factory=list)
    qualified: list[QualifiedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Rows that could carry a friendly name and do not, with a suggestion. Not a
    # warning: a missing nickname blocks nothing, and dressing it as a problem
    # trains people past the warnings that matter.
    naming_suggestions: list[dict] = field(default_factory=list)
    # Value-level grouping across vendors, for procurement. Separate from
    # `grouped` rather than replacing it, because the MPN grouping is the one
    # that is always safe and this one is the one that needs eyes.
    equivalence: list[EquivalenceLine] = field(default_factory=list)
    # Parts that could not be shown equivalent to anything, each with the reason.
    # Kept visible so an absent line reads as "not confirmed" rather than
    # "nothing to consolidate".
    not_grouped: list[dict] = field(default_factory=list)
    schema_version: int = 1

    @property
    def line_count(self) -> int:
        return len(self.grouped)

    @property
    def part_count(self) -> int:
        return len(self.qualified)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "configuration": self.configuration,
            "schema_version": self.schema_version,
            "warnings": self.warnings,
            "naming_suggestions": self.naming_suggestions,
            "equivalence": [e.to_dict() for e in self.equivalence],
            "not_grouped": self.not_grouped,
            "line_count": self.line_count,
            "part_count": self.part_count,
            "grouped": [g.to_dict() for g in self.grouped],
            "qualified": [q.to_dict() for q in self.qualified],
        }


# What a refdes class is, in words. Used only to build a suggestion — a wrong
# guess costs a rejected suggestion, which is the cheapest possible failure.
_CLASS_WORDS = {
    "R": "resistor", "RN": "resistor network", "C": "capacitor", "L": "inductor",
    "D": "diode", "LED": "indicator", "Q": "transistor", "U": "integrated circuit",
    "J": "connector", "P": "connector", "SW": "switch", "F": "fuse",
    "FB": "ferrite bead", "Y": "crystal", "X": "oscillator", "T": "transformer",
    "TP": "test point", "K": "relay", "M": "motor", "BT": "battery",
    "ANT": "antenna", "JP": "jumper", "MH": "mounting hole",
}


# Words that carry no information in a name. Dropping them is what turns
# "humidity and temperature sensor" into a compound rather than a sentence.
_STOPWORDS = {
    "a", "an", "and", "the", "for", "with", "of", "to", "in", "on", "or", "per",
    "type", "series", "part", "generic",
}

# Description words that already say what class of thing this is, so appending
# the class word would only repeat it. "main microcontroller" is a finished
# name; "main microcontroller integrated circuit" is a name with a stutter.
_IMPLIES_CLASS = {
    "microcontroller", "mcu", "sensor", "regulator", "converter", "amplifier",
    "transceiver", "controller", "driver", "oscillator", "crystal", "connector",
    "header", "receptacle", "socket", "switch", "relay", "fuse", "antenna",
    "capacitor", "resistor", "inductor", "diode", "transistor", "battery",
    "led", "bead", "transformer", "jumper",
}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^A-Za-z0-9.]+", text or "") if w]


def _content_words(text: str) -> list[str]:
    """The words worth keeping, in the case they were written in.

    Casing is preserved for anything carrying a digit or written in caps —
    ``100nF``, ``X7R``, ``I2C``, ``3V3``. Those are part values and standards,
    not prose, and lowercasing them makes a suggestion look wrong enough that
    people stop reading the suggestions.
    """
    out: list[str] = []
    seen: set[str] = set()
    for w in _words(text):
        low = w.lower()
        if low in _STOPWORDS or len(w) < 2 or low in seen:
            continue
        seen.add(low)
        technical = any(c.isdigit() for c in w) or (w.isupper() and len(w) > 1)
        out.append(w if technical else low)
    return out


def suggest_friendly_name(row: QualifiedRow, *, style: str = "english") -> str:
    """A name that says what the part is for, without anyone having to look it up.

    Built the way German technical compounds are: length is immaterial, and
    self-contained descriptiveness is the requirement. ``Doppelkupplungsgetriebe``
    tells an engineer what the thing does without knowing the phrase; "DCT" does
    not. So the suggestion composes function and position rather than reaching
    for a term of art or an abbreviation.

    English by default. ``style="german"`` produces the actual compound, which is
    a real option rather than a joke — a closed compound cannot be mistaken for
    two separate labels the way a spaced English one can.

    Only ever a suggestion. Nothing here goes into a BOM without a person
    accepting it, because a plausible wrong name is worse than an empty column:
    the empty column gets asked about.
    """
    cls = row.ref.component_class
    kind = _CLASS_WORDS.get(cls, "component")
    role = _content_words(row.description)

    # If the description already names the kind of thing, the class word would
    # only stutter — "main microcontroller" is finished, "main microcontroller
    # integrated circuit" is not better for being longer.
    already_said = any(w.lower() in _IMPLIES_CLASS for w in role)
    parts = role[:4] if already_said else (role[:3] + kind.split())

    if not parts:
        parts = kind.split()

    if style == "german":
        # A closed compound, which is the point: it cannot be misread as two
        # separate labels the way a spaced English name can.
        return "".join(w.capitalize() for w in parts + [row.board])

    name = " ".join(parts)
    return f"{name[0].upper()}{name[1:]} on {row.board}"


def _row_ref(designator: Designator, raw: dict) -> ComponentRef | None:
    refdes = str(raw.get("refdes") or raw.get("local_id") or "").strip()
    if not refdes:
        return None
    try:
        return qualify(designator, refdes)
    except Exception:
        return None


def aggregate(
    man: ProjectManifest,
    cfg: Configuration,
    board_boms: dict[str, dict],
    designators: dict[str, Designator],
    *,
    naming_style: str = "english",
) -> AggregateBom:
    """Build one BOM for a configuration from its boards' BOMs.

    ``designators`` maps board name to where that board sits, because the BOM
    prefix is positional and the board's own name is not.
    """
    out = AggregateBom(project_id=man.project_id, configuration=cfg.name)
    present = [b for b in cfg.boards]

    for board in present:
        if board not in board_boms:
            out.warnings.append(
                f"{board} is in this configuration but has no BOM — run Stage 1 for it "
                "before aggregating, or its parts are simply missing from the order."
            )
            continue
        designator = designators.get(board)
        if designator is None:
            out.warnings.append(
                f"{board} has no designator, so its parts cannot be placed in the "
                "project BOM. Give it one in project.md."
            )
            continue
        for raw in board_boms[board].get("rows", []):
            ref = _row_ref(designator, raw)
            if ref is None:
                out.warnings.append(
                    f"{board}: a BOM row has no usable refdes and was left out "
                    f"({raw.get('mpn') or 'no mpn'})."
                )
                continue
            out.qualified.append(
                QualifiedRow(
                    ref=ref,
                    board=board,
                    mpn=str(raw.get("mpn") or ""),
                    manufacturer=str(raw.get("manufacturer") or ""),
                    package=str(raw.get("package") or ""),
                    description=str(raw.get("description") or ""),
                    friendly_name=str(raw.get("friendly_name") or ""),
                    spec=passives.extract(raw),
                )
            )

    out.qualified.sort(key=lambda q: q.ref.sort_key())
    _group(out)
    _equivalence(out)
    _suggest_names(out, naming_style)
    return out


def _group(bom: AggregateBom) -> None:
    """Collapse to one line per part number, keeping every place it is used."""
    by_mpn: dict[str, GroupedRow] = {}
    for q in bom.qualified:
        key = q.mpn.strip().upper()
        if not key:
            # A part with no MPN cannot be ordered, and grouping several of them
            # under one blank line would hide how many there are.
            bom.warnings.append(
                f"{q.designator} has no MPN and cannot be ordered as it stands."
            )
            continue
        g = by_mpn.get(key)
        if g is None:
            g = GroupedRow(
                mpn=q.mpn,
                manufacturer=q.manufacturer,
                package=q.package,
                description=q.description,
                friendly_name=q.friendly_name,
            )
            by_mpn[key] = g
        g.designators.append(q.designator)
        if q.board not in g.boards:
            g.boards.append(q.board)
        # First non-empty wins, so one board naming a part does not get erased
        # by another that left the field blank.
        g.friendly_name = g.friendly_name or q.friendly_name
        g.description = g.description or q.description
    bom.grouped = sorted(by_mpn.values(), key=lambda g: (g.mpn.upper(),))


def _equivalence(bom: AggregateBom) -> None:
    """Group across vendors by what a part *is*, not who made it.

    Only where equivalence can be demonstrated. Everything else lands in
    ``not_grouped`` with the reason, because the dangerous failure here is a
    line that silently absorbed a part it should not have — after the merge
    there is nothing left to notice.
    """
    lines: dict[str, EquivalenceLine] = {}
    for q in bom.qualified:
        spec = q.spec
        if spec is None:
            continue
        cls = q.ref.component_class
        key = passives.equivalence_key(spec, cls)
        if key is None:
            bom.not_grouped.append(
                {
                    "designator": q.designator,
                    "mpn": q.mpn,
                    "reason": passives.why_unmergeable(spec, cls),
                }
            )
            continue
        line = lines.get(key)
        if line is None:
            line = EquivalenceLine(
                key=key,
                value_text=spec.value_text or (f"{spec.value:g}{spec.unit}" if spec.value else ""),
                package=spec.package,
            )
            lines[key] = line
        line.quantity += 1
        line.designators.append(q.designator)
        if q.mpn and q.mpn not in line.mpns:
            line.mpns.append(q.mpn)
        line.inferred_from_text = line.inferred_from_text or spec.is_inferred
        line.provenance = passives.weakest_provenance([line.provenance, spec.provenance])
        if len(line.designators) == 1:
            line.provenance = spec.provenance

    # Only lines that actually consolidate something are worth showing: a
    # one-vendor line is the MPN grouping again under a different heading.
    bom.equivalence = sorted(
        (l for l in lines.values() if len(l.mpns) > 1),
        key=lambda l: (l.package, l.value_text),
    )


def _suggest_names(bom: AggregateBom, style: str) -> None:
    """Note what has no friendly name, and offer one.

    Info, never a warning. The gap is real but it blocks nothing, and a commit
    hook is the right place for it to surface — that is the moment someone is
    already thinking about the part and can answer in a second.
    """
    # Driven off the grouped rows, not the qualified ones. A part number is one
    # naming question however many times it is placed, and naming R26 answers it
    # for R27 as well — asking again about the same part because a second
    # instance happens to have an empty field is exactly the noise that gets
    # suggestions ignored.
    by_mpn: dict[str, QualifiedRow] = {}
    for q in bom.qualified:
        by_mpn.setdefault(q.mpn.strip().upper(), q)

    for g in bom.grouped:
        if g.friendly_name:
            continue
        q = by_mpn.get(g.mpn.strip().upper())
        if q is None:
            continue
        bom.naming_suggestions.append(
            {
                "severity": "info",
                "mpn": q.mpn,
                "example_designator": q.designator,
                "suggestion": suggest_friendly_name(q, style=style),
                "message": (
                    f"{q.mpn} has no friendly name. Optional — nothing is blocked by "
                    "it — but a name here is what makes this row readable to whoever "
                    "is not the person who chose the part."
                ),
            }
        )


def aggregate_all(
    man: ProjectManifest,
    board_boms: dict[str, dict],
    designators: dict[str, Designator],
    *,
    configurations: Iterable[Configuration] | None = None,
    naming_style: str = "english",
) -> dict[str, AggregateBom]:
    """One aggregated BOM per configuration.

    Per configuration because that is what gets ordered: the parts for `minimal`
    are a different purchase order from `full`, and a single combined BOM would
    describe a build nobody makes.
    """
    configs = list(configurations if configurations is not None else man.configurations)
    return {
        c.name: aggregate(man, c, board_boms, designators, naming_style=naming_style)
        for c in configs
    }
