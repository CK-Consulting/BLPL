"""The positional naming scheme: project, board, sub-board, revision."""

from __future__ import annotations

import pytest

from blpl.core.designators import (
    Designator,
    DesignatorError,
    Segment,
    is_bare_refdes,
    parse,
    parse_component,
    qualify,
)


@pytest.mark.parametrize(
    "text,proj,depth",
    [
        ("1.1r0", 1, 1),       # unreleased — the common case
        ("2.1r3", 2, 1),       # single-board project, project rev 2, board rev 3
        ("1.1r5", 1, 1),
        ("1.2r19", 1, 1),
        ("1.2r19.1r2", 1, 2),  # board 2's first sub-board
        ("1.2r19.2r5", 1, 2),  # …and its second
    ],
)
def test_the_specified_examples_round_trip(text, proj, depth):
    d = parse(text)
    assert (d.project_revision, d.depth) == (proj, depth)
    assert str(d) == text


def test_a_single_board_project_still_names_its_board():
    """Not a special case with a shorter name — the ordinary case with one
    board, so downstream code has one shape to handle."""
    d = parse("2.1r3")
    assert d.depth == 1 and d.board == Segment(1, 3)


def test_a_project_revision_alone_is_incomplete():
    with pytest.raises(DesignatorError, match="no board"):
        parse("2")


def test_position_counts_from_one_but_revision_counts_from_zero():
    """r0 is where almost everything lives. A revision number records that a
    design was released for a production run, not that a file changed — so
    requiring r1 would force every in-progress board to claim a production
    revision it does not have."""
    assert str(parse("1.1r0")) == "1.1r0"
    assert str(parse("1.2r0.1r0")) == "1.2r0.1r0"
    for bad in ("0.1r1", "1.0r1"):
        with pytest.raises(DesignatorError):
            parse(bad)


def test_each_level_revises_independently():
    """A new enclosure takes the project from 1 to 2 and leaves every board
    where it was. The path is a compatibility statement, not a new identity for
    the same physical thing."""
    d = parse("1.2r19.2r5")
    rolled = d.at_project_revision(2)
    assert str(rolled) == "2.2r19.2r5"
    assert rolled.path == d.path


def test_leading_zeros_normalise():
    assert parse("1.02r19") == parse("1.2r19")


def test_parent_walks_up_one_level():
    d = parse("1.2r19.2r5")
    assert str(d.parent()) == "1.2r19"
    assert parse("1.2r19").parent() is None


def test_rolling_the_project_keeps_the_board_numbering():
    """Board 2 is board 2 for the life of the project; a project revision does
    not renumber what is in it."""
    assert str(parse("1.2r19").at_project_revision(2)) == "2.2r19"


# -- component references ----------------------------------------------------


@pytest.mark.parametrize(
    "text,board,refdes",
    [
        ("1.2r19.R26", "1.2r19", "R26"),
        ("1.2r19.J12", "1.2r19", "J12"),
        ("1.2r19.2r5.C49", "1.2r19.2r5", "C49"),
    ],
)
def test_component_references_split_without_a_delimiter(text, board, refdes):
    """A positional segment starts with a digit, a refdes with a letter — so
    nothing has to be escaped to tell them apart."""
    c = parse_component(text)
    assert str(c.designator) == board
    assert c.refdes == refdes
    assert str(c) == text


def test_the_board_local_form_is_bare():
    """What fits on a silkscreen, and what the person holding the board reads."""
    c = parse_component("1.2r19.R26")
    assert c.local == "R26"


def test_qualifying_is_the_only_place_a_prefix_appears():
    c = qualify(parse("1.2r19"), "r26")
    assert str(c) == "1.2r19.R26"
    assert c.local == "R26"


def test_multi_letter_classes_survive():
    """RN12 is a resistor network, not a malformed segment."""
    c = parse_component("1.2r19.RN12")
    assert c.component_class == "RN" and c.sequence == 12


def test_something_that_is_neither_is_refused():
    with pytest.raises(DesignatorError):
        parse_component("1.2r19.R2r5")


def test_a_bare_refdes_is_recognised_as_board_local():
    assert is_bare_refdes("R26") and not is_bare_refdes("1.2r19.R26")
    with pytest.raises(DesignatorError, match="bare refdes"):
        parse_component("R26")


# -- aggregation -------------------------------------------------------------


def test_a_mixed_depth_bom_sorts_without_raising():
    """Flattened keys put a revision number opposite a component class and the
    comparison raises. This is the regression guard for that."""
    rows = [
        "1.2r19.2r5.C49",
        "1.2r19.R26",
        "1.1r5.R26",
        "1.2r19.J12",
        "1.2r19.1r2.R1",
        "2.1r3.C1",
    ]
    refs = [parse_component(r) for r in rows]
    got = [str(c) for c in sorted(refs, key=lambda r: r.sort_key())]
    assert got == [
        "1.1r5.R26",
        "1.2r19.J12",
        "1.2r19.R26",
        "1.2r19.1r2.R1",
        "1.2r19.2r5.C49",
        "2.1r3.C1",
    ]


def test_a_board_sorts_before_what_plugs_into_it():
    board = parse_component("1.2r19.R26").sort_key()
    sub = parse_component("1.2r19.1r2.R1").sort_key()
    assert board < sub


def test_the_same_part_on_two_boards_stays_two_rows():
    """The whole point of the prefix: one row with a quantity nobody can place
    is worse than four rows that each trace back."""
    a, b = parse_component("1.1r5.R26"), parse_component("1.2r19.R26")
    assert a != b and a.local == b.local == "R26"
