"""Naming things so that two of the same part on different boards stay apart.

Words do not survive this problem. "Configuration", "revision", "variant",
"sub-assembly" all mean something slightly different to every engineer who has
used them, and a BOM that leans on them needs a glossary before it can be read.
A positional scheme needs none: the shape of the string says where a thing sits.

    <project revision>.<board>r<board revision>[.<sub>r<sub revision>]…

Read left to right, each element narrows: which revision of the project, which
board within it, which sub-board within that. Every project has at least one
board, so the second element is always present — a single-board project is not a
special case with a shorter name, it is the ordinary case with one board.

    2.1r3          project rev 2, its only board, board rev 3
    1.1r5          project rev 1, board 1, board rev 5
    1.2r19         project rev 1, board 2, board rev 19
    1.2r19.1r2     …and that board's first sub-board, sub rev 2
    1.2r19.2r5     …and its second, sub rev 5

**Where the prefix appears is the point.** On the board itself a part is ``R26``,
because that is what fits on a silkscreen and what someone holding the board
reads. It only grows a prefix when it is aggregated up to the project, where
``1.2r19.R26`` and ``1.2r19.2r5.C49`` are unambiguous and sort into board order
for free. The same part number on four boards stays four rows that can each be
traced back, rather than one row with a quantity nobody can place.

A component reference is a designator plus a refdes, and the two never blur: a
positional segment starts with a digit (``2r19``), a refdes with a letter
(``R26``). Nothing has to be escaped or delimited specially to tell them apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "2r19" — an index within its parent, and that item's own revision.
_SEGMENT_RE = re.compile(r"^(?P<index>\d+)r(?P<rev>\d+)$")
# "R26", "J12", "C49" — a letter class then a sequence number.
_REFDES_RE = re.compile(r"^(?P<cls>[A-Za-z]+)(?P<num>\d+)$")


class DesignatorError(ValueError):
    """A designator or component reference could not be read."""


@dataclass(frozen=True)
class Segment:
    """One board or sub-board: its position, and which revision of it."""

    index: int
    revision: int

    def __post_init__(self) -> None:
        # Everything in this scheme counts from one — the first board is board 1
        # and its first revision is r1. A zero would read as "no board" or "not
        # yet revised", both of which are states the string cannot mean.
        if self.index < 1:
            raise DesignatorError(f"board index starts at 1, got {self.index}")
        if self.revision < 1:
            raise DesignatorError(f"revision starts at 1, got r{self.revision}")

    def __str__(self) -> str:
        return f"{self.index}r{self.revision}"


@dataclass(frozen=True)
class Designator:
    """Where something sits: project revision, board, and any sub-boards."""

    project_revision: int
    path: tuple[Segment, ...]

    def __post_init__(self) -> None:
        if self.project_revision < 1:
            raise DesignatorError("project revision starts at 1")
        if not self.path:
            # Every project has a board even when it has only one, so a
            # designator with no board is not a shorter name — it is incomplete.
            raise DesignatorError("a designator needs at least a board")

    def __str__(self) -> str:
        return ".".join([str(self.project_revision), *(str(s) for s in self.path)])

    @property
    def board(self) -> Segment:
        return self.path[0]

    @property
    def subs(self) -> tuple[Segment, ...]:
        return self.path[1:]

    @property
    def depth(self) -> int:
        """1 for a board, 2 for a sub-board, and so on."""
        return len(self.path)

    @property
    def is_sub_board(self) -> bool:
        return self.depth > 1

    def parent(self) -> "Designator | None":
        """The thing this plugs into, or None for a board."""
        if not self.is_sub_board:
            return None
        return Designator(self.project_revision, self.path[:-1])

    def child(self, index: int, revision: int) -> "Designator":
        return Designator(self.project_revision, self.path + (Segment(index, revision),))

    def at_project_revision(self, revision: int) -> "Designator":
        """The same position under a different project revision.

        A project revision does not renumber its boards — board 2 is board 2
        across the life of the project — so rolling the project forward keeps
        the path and changes only the first element.
        """
        return Designator(revision, self.path)

    def sort_key(self) -> tuple:
        """Orders a BOM the way someone reads it: project, board, sub, revision.

        The path is nested rather than flattened. Flattened, a board and a
        sub-board produce keys of different length whose elements no longer line
        up, and sorting a mixed-depth BOM compares a revision number against a
        component class and raises. Nested, a shorter path simply sorts before a
        longer one that shares its prefix — which is also the order a reader
        expects: the board, then the things plugged into it.
        """
        return (self.project_revision, tuple((s.index, s.revision) for s in self.path))


def parse(text: str) -> Designator:
    """Read a designator, or say precisely what is wrong with it."""
    raw = (text or "").strip()
    if not raw:
        raise DesignatorError("empty designator")
    parts = raw.split(".")
    if len(parts) < 2:
        raise DesignatorError(
            f"{raw!r} names a project revision but no board. Every project has at "
            "least one board, so a designator has at least two elements."
        )
    head, *rest = parts
    if not head.isdigit():
        raise DesignatorError(f"{raw!r}: {head!r} is not a project revision")
    segments: list[Segment] = []
    for seg in rest:
        m = _SEGMENT_RE.match(seg)
        if not m:
            raise DesignatorError(
                f"{raw!r}: {seg!r} is not '<index>r<revision>' — e.g. '2r19'"
            )
        segments.append(Segment(int(m.group("index")), int(m.group("rev"))))
    return Designator(int(head), tuple(segments))


@dataclass(frozen=True)
class ComponentRef:
    """One part, and the board it is actually on."""

    designator: Designator
    refdes: str

    def __str__(self) -> str:
        """The aggregated form — what a project-level BOM row is keyed on."""
        return f"{self.designator}.{self.refdes}"

    @property
    def local(self) -> str:
        """What is printed on the board.

        Deliberately bare. A silkscreen has room for ``R26`` and not for
        ``1.2r19.R26``, and the person holding the board is not disambiguating
        against another board they cannot see.
        """
        return self.refdes

    @property
    def component_class(self) -> str:
        m = _REFDES_RE.match(self.refdes)
        return m.group("cls").upper() if m else ""

    @property
    def sequence(self) -> int:
        m = _REFDES_RE.match(self.refdes)
        return int(m.group("num")) if m else 0

    def sort_key(self) -> tuple:
        """Board order, then class, then number — how a BOM is read."""
        return (*self.designator.sort_key(), self.component_class, self.sequence)

    # (the designator's key is two elements — an int and a nested tuple — so the
    # class and sequence always land at the same positions regardless of depth)


def parse_component(text: str) -> ComponentRef:
    """Read an aggregated component reference such as ``1.2r19.2r5.C49``.

    The split is unambiguous without any extra delimiter: positional segments
    start with a digit, a refdes starts with a letter.
    """
    raw = (text or "").strip()
    if "." not in raw:
        raise DesignatorError(
            f"{raw!r} is a bare refdes. A project-level reference needs the board "
            "it is on — e.g. '1.2r19.R26'."
        )
    head, _, refdes = raw.rpartition(".")
    if not _REFDES_RE.match(refdes):
        raise DesignatorError(
            f"{raw!r}: {refdes!r} is not a refdes like 'R26'. Positional segments "
            "start with a digit, refdes with a letter."
        )
    return ComponentRef(parse(head), refdes)


def qualify(designator: Designator, refdes: str) -> ComponentRef:
    """Attach a board-local refdes to the board it sits on.

    This is the aggregation step, and the only place a prefix is added. Anything
    reading a single board's BOM should never see one.
    """
    if not _REFDES_RE.match(refdes.strip()):
        raise DesignatorError(f"{refdes!r} is not a refdes like 'R26'")
    return ComponentRef(designator, refdes.strip().upper())


def is_bare_refdes(text: str) -> bool:
    """Whether this is a board-local reference rather than an aggregated one."""
    return bool(_REFDES_RE.match((text or "").strip()))
