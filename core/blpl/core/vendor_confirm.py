"""Checking a BOM's electrical attributes against what distributors say.

The equivalence grouping in ``bom_aggregate`` decides which parts can be ordered
as one line, and it decides that from attributes — voltage, tolerance,
dielectric, power. Where those came from therefore matters as much as what they
say. A number read out of a design document is a statement of intent; a number
from a distributor is a statement about the part that will arrive in the box.
Confirmation is the step that turns the first into the second.

The interesting case is not agreement. It is **disagreement**, and there is
exactly one honest thing to do with it: report it and upgrade nothing.

    The design says 50 V. Mouser says the MPN is rated 16 V.

That is not a data-quality problem to resolve by preferring a source. It means
the part chosen does not meet the requirement written down beside it, or the MPN
is wrong, or the vendor page is for a different variant. All three are things a
person has to look at, and all three are invisible the moment either side is
allowed to quietly win. So a conflicting attribute keeps whatever it had, gains
no vendor provenance, and blocks the part from equivalence grouping — because a
part whose rating is in dispute cannot be shown equivalent to anything.

Distributors disagreeing with *each other* is treated the same way. Two vendors
describing one MPN differently is evidence about the MPN, not noise to average.

What confirmation does do, quietly and usefully: fill blanks the design document
never stated, and raise provenance to ``vendor`` where a vendor agrees with what
was already there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .passives import PROVENANCE_RANK

# Attributes a distributor can speak to. Anything outside this is not something
# a parts API is authoritative about, whatever it happens to return.
CONFIRMABLE = ("value", "tolerance", "voltage_v", "power_w", "dielectric", "safety_class")

# Numbers from two sources rarely match to the last bit — a tolerance of 1 and
# 1.0, a voltage of 50 and 50.0. Compared with a relative epsilon so identical
# ratings are not reported as a conflict.
_EPSILON = 1e-9


@dataclass
class AttributeOutcome:
    attribute: str
    designed: object = None
    vendor: object = None
    state: str = ""           # confirmed | filled | conflict | unavailable
    sources: tuple[str, ...] = ()

    @property
    def is_conflict(self) -> bool:
        return self.state == "conflict"

    def to_dict(self) -> dict:
        return {
            "attribute": self.attribute,
            "designed": self.designed,
            "vendor": self.vendor,
            "state": self.state,
            "sources": list(self.sources),
        }


@dataclass
class RowConfirmation:
    local_id: str
    mpn: str
    outcomes: list[AttributeOutcome] = field(default_factory=list)
    # Distributors that were asked and had nothing, kept separate from ones
    # nobody asked — "not found" and "no key configured" are different facts.
    checked: tuple[str, ...] = ()

    @property
    def conflicts(self) -> list[AttributeOutcome]:
        return [o for o in self.outcomes if o.is_conflict]

    @property
    def blocked(self) -> bool:
        """Whether this row may be grouped for ordering.

        A disputed rating is exactly the case the grouping rule already covers:
        it cannot be *shown* to match anything, so it does not group.
        """
        return bool(self.conflicts)

    def to_dict(self) -> dict:
        return {
            "local_id": self.local_id,
            "mpn": self.mpn,
            "checked": list(self.checked),
            "blocked": self.blocked,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


@dataclass
class ConfirmationReport:
    project_id: str
    rows: list[RowConfirmation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def conflicted(self) -> list[RowConfirmation]:
        return [r for r in self.rows if r.conflicts]

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            # Conflicts first and counted, because they are the reason to read
            # this file at all and the rest of it is long.
            "conflict_count": len(self.conflicted),
            "warnings": self.warnings,
            "rows": [r.to_dict() for r in self.rows],
        }


def _same(a: object, b: object) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        big = max(abs(float(a)), abs(float(b)), 1.0)
        return abs(float(a) - float(b)) / big < _EPSILON
    return str(a).strip().upper() == str(b).strip().upper()


def _vendor_view(hits: list[dict], attribute: str) -> tuple[object, tuple[str, ...], bool]:
    """What the distributors say about one attribute.

    Returns the value, who said it, and whether they disagreed among
    themselves. Vendors contradicting each other about one MPN is evidence
    about that MPN, not noise to average away, so it is surfaced rather than
    resolved.
    """
    seen: list[tuple[str, object]] = []
    for hit in hits:
        attrs = hit.get("attributes") or {}
        if attribute in attrs and attrs[attribute] not in (None, ""):
            seen.append((str(hit.get("distributor") or "?"), attrs[attribute]))
    if not seen:
        return None, (), False
    first = seen[0][1]
    disagree = any(not _same(first, v) for _, v in seen[1:])
    return first, tuple(d for d, _ in seen), disagree


def confirm_row(row: dict, hits: list[dict]) -> RowConfirmation:
    """Compare one BOM row's attributes against what distributors report."""
    out = RowConfirmation(
        local_id=str(row.get("local_id") or row.get("refdes") or ""),
        mpn=str(row.get("mpn") or ""),
        checked=tuple(str(h.get("distributor") or "?") for h in hits),
    )
    for attr in CONFIRMABLE:
        designed = row.get(attr)
        designed = None if designed in ("", None) else designed
        vendor, sources, disagree = _vendor_view(hits, attr)

        if vendor is None:
            out.outcomes.append(
                AttributeOutcome(attribute=attr, designed=designed, state="unavailable")
            )
            continue
        if disagree:
            out.outcomes.append(
                AttributeOutcome(
                    attribute=attr, designed=designed, vendor=vendor,
                    state="conflict", sources=sources,
                )
            )
            continue
        if designed is None:
            out.outcomes.append(
                AttributeOutcome(
                    attribute=attr, vendor=vendor, state="filled", sources=sources
                )
            )
            continue
        state = "confirmed" if _same(designed, vendor) else "conflict"
        out.outcomes.append(
            AttributeOutcome(
                attribute=attr, designed=designed, vendor=vendor,
                state=state, sources=sources,
            )
        )
    return out


def apply(row: dict, confirmation: RowConfirmation) -> dict:
    """A copy of the row with what the vendors settled, and nothing they didn't.

    Fills blanks and raises provenance where a vendor agreed. Leaves a disputed
    attribute exactly as the design document had it — writing the vendor's value
    over it would resolve, by fiat, the question a person needs to answer.
    """
    updated = dict(row)
    filled_or_confirmed = False
    for o in confirmation.outcomes:
        if o.state == "filled":
            updated[o.attribute] = o.vendor
            filled_or_confirmed = True
        elif o.state == "confirmed":
            filled_or_confirmed = True

    if confirmation.conflicts:
        # No provenance upgrade while anything is disputed. A row is not
        # "vendor-confirmed" because most of it was.
        updated["attribute_conflicts"] = [o.attribute for o in confirmation.conflicts]
        return updated

    updated.pop("attribute_conflicts", None)
    if filled_or_confirmed:
        current = str(row.get("attribute_provenance") or "unknown")
        # Never downgrade: a manually entered value that a vendor happens to
        # agree with does not become less trustworthy for having been checked.
        if PROVENANCE_RANK.index("vendor") >= PROVENANCE_RANK.index(
            current if current in PROVENANCE_RANK else "unknown"
        ):
            updated["attribute_provenance"] = "vendor"
    return updated


def confirm_bom(
    bom: dict, lookups: dict[str, list[dict]]
) -> tuple[dict, ConfirmationReport]:
    """Confirm every row of a BOM, returning the updated BOM and a report.

    ``lookups`` maps MPN to the distributor hits for it — the shape
    ``blpl.agent.tools.parts.search_parts`` already produces.
    """
    report = ConfirmationReport(project_id=str(bom.get("project_id") or ""))
    rows: list[dict] = []
    for row in bom.get("rows", []):
        mpn = str(row.get("mpn") or "")
        hits = lookups.get(mpn) or lookups.get(mpn.upper()) or []
        if not hits:
            rows.append(dict(row))
            report.warnings.append(
                f"{row.get('local_id') or mpn}: no distributor answered for {mpn or 'a row with no MPN'}, "
                "so its attributes are unconfirmed."
            )
            continue
        conf = confirm_row(row, hits)
        report.rows.append(conf)
        rows.append(apply(row, conf))

    updated = dict(bom)
    updated["rows"] = rows
    for r in report.conflicted:
        names = ", ".join(o.attribute.replace("_", " ") for o in r.conflicts)
        report.warnings.append(
            f"{r.local_id} ({r.mpn}): the design and the distributor disagree on {names}. "
            "Either the part does not meet the requirement written beside it, the MPN is "
            "wrong, or the vendor page is a different variant — all worth a look, and none "
            "of them fixed by preferring one source."
        )
    return updated, report
