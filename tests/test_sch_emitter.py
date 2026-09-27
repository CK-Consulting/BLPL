"""How the schematic emitter arranges what it draws.

Connectivity on these sheets is net labels: every connected pin gets a short
stub wire ending in a label, and same-named labels are what join pins. That is
valid and ERC agrees — but it means there is no wire between components to
follow with your eye, so where a symbol sits on the sheet is the only thing
that tells a reader which parts belong together.
"""

from __future__ import annotations

# --- reading order ----------------------------------------------------------
#
# The grid followed BOM order, so a decoupling capacitor could sit a dozen
# cells from the pin it decouples. Every connection on these sheets is a net
# label, so there is no wire to follow with your eye — which is what made them
# read as "a grid of symbols" rather than a schematic.


def _comp(x=None, y=None):
    c = {"lib_symbol": "Device:C"}
    if x is not None:
        c["placement"] = {"x": x, "y": y}
    return c


def test_symbols_follow_the_boards_grouping_not_bom_order() -> None:
    from blpl.emitter import sch

    # BOM order interleaves two clusters; the board separates them.
    components = {
        "U1": _comp(5, 5),
        "R_FAR": _comp(80, 60),
        "C_U1_VDD": _comp(6, 5),
        "C_FAR": _comp(81, 60),
    }
    placements = [(r, 1) for r in components]
    ordered = [r for r, _ in sch._reading_order(placements, components, bands=2)]

    assert ordered.index("C_U1_VDD") - ordered.index("U1") == 1
    assert ordered.index("C_FAR") - ordered.index("R_FAR") == 1
    assert set(ordered[:2]) == {"U1", "C_U1_VDD"}


def test_a_part_without_a_placement_keeps_its_order_and_goes_last() -> None:
    """An unplaced part must never be dropped or shuffled among the others —
    stage 5 refuses rather than placing what will not fit, so this is a real
    case and not a hypothetical."""
    from blpl.emitter import sch

    components = {"U1": _comp(5, 5), "X_NOFIT": _comp(), "C1": _comp(6, 5),
                  "Y_NOFIT": _comp()}
    placements = [(r, 1) for r in components]
    ordered = [r for r, _ in sch._reading_order(placements, components)]

    assert ordered[-2:] == ["X_NOFIT", "Y_NOFIT"]
    assert set(ordered) == set(components)


def test_multi_unit_symbols_keep_every_unit() -> None:
    from blpl.emitter import sch

    components = {"U1": _comp(5, 5), "U2": _comp(40, 40)}
    placements = [("U1", 1), ("U1", 2), ("U1", 3), ("U2", 1)]
    ordered = sch._reading_order(placements, components)
    assert sorted(ordered) == sorted(placements)


def test_no_placements_at_all_leaves_the_order_alone() -> None:
    from blpl.emitter import sch

    components = {"A": _comp(), "B": _comp()}
    placements = [("A", 1), ("B", 1)]
    assert sch._reading_order(placements, components) == placements
