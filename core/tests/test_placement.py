"""Components on the board, inside it, not on top of each other.

What this replaced was a 15 mm grid that sized every part the same and let its
rows run off the bottom edge. On example-handheld it put 94 of core's 130
parts outside the board and sb-ant's column reached y=455 mm on a 35 mm board —
and it reported success every time, so the overflow only showed up much later
as a board nobody could route. Most of these tests exist because of that: the
properties worth pinning are the ones whose absence was invisible.
"""

from __future__ import annotations

import pytest

from blpl.core import placement


def _comp(fp: str | None = None) -> dict:
    return {"footprint": fp} if fp else {}


def _net(*refs: str) -> dict:
    return {"pads": [[r, "1"] for r in refs]}


def _boxes(result: placement.Result):
    """What each part actually occupies — origin plus courtyard offset, not the
    origin alone. Checking the origin is how an overlap-free board acquires
    overlapping courtyards."""
    return [(r, *placement.occupied_box(result, r)) for r in result.placements]


def _overlaps(result: placement.Result) -> list[tuple[str, str]]:
    out = []
    items = _boxes(result)
    for i, (a, ax0, ay0, ax1, ay1) in enumerate(items):
        for (b, bx0, by0, bx1, by1) in items[i + 1:]:
            if ax0 < bx1 - 1e-9 and bx0 < ax1 - 1e-9 and ay0 < by1 - 1e-9 and by0 < ay1 - 1e-9:
                out.append((a, b))
    return out


def _outside(result: placement.Result, w: float, h: float) -> list[str]:
    return [r for (r, x0, y0, x1, y1) in _boxes(result)
            if x0 < -1e-9 or y0 < -1e-9 or x1 > w + 1e-9 or y1 > h + 1e-9]


def test_nothing_overlaps():
    comps = {f"R{i}": _comp() for i in range(20)}
    r = placement.place(comps, {}, (60, 60))
    assert r.ok
    assert _overlaps(r) == []


def test_nothing_lands_outside_the_board():
    """The grid's actual failure, and the reason this module exists."""
    comps = {f"R{i}": _comp() for i in range(20)}
    r = placement.place(comps, {}, (60, 60))
    assert _outside(r, 60, 60) == []


def test_a_board_too_small_refuses_instead_of_overflowing():
    """Silence was the bug. A part that does not fit must be named."""
    comps = {f"R{i}": _comp() for i in range(200)}
    r = placement.place(comps, {}, (12, 12))
    assert not r.ok
    assert r.unplaced, "parts that did not fit must be reported"
    assert _outside(r, 12, 12) == [], "refusing beats placing off the board"
    assert all("no free" in why for why in r.unplaced.values())


def test_placement_is_deterministic():
    """Two runs of a build must produce the same board."""
    comps = {f"R{i}": _comp() for i in range(15)}
    nets = {"N1": _net("R1", "R2"), "N2": _net("R3", "R4", "R5")}
    a = placement.place(comps, nets, (50, 50))
    b = placement.place(comps, nets, (50, 50))
    assert {k: v.as_dict() for k, v in a.placements.items()} == \
           {k: v.as_dict() for k, v in b.placements.items()}


def test_connected_parts_end_up_nearer_than_unconnected_ones():
    """The one thing a placer can actually promise the router."""
    comps = {f"R{i}": _comp() for i in range(12)}
    tied = {"N1": _net("R0", "R1"), "N2": _net("R0", "R2"), "N3": _net("R0", "R3")}
    r = placement.place(comps, tied, (60, 60))
    hub = r.placements["R0"]

    def dist(ref):
        p = r.placements[ref]
        return abs(p.x - hub.x) + abs(p.y - hub.y)

    # Mean, not max-vs-min: positions are discrete, so the nearest free cell
    # left over for an unconnected part can tie with the furthest of three
    # neighbours. The property worth asserting is that connection pulls parts
    # in, not that no single tie is ever possible.
    neighbours = sum(dist(x) for x in ("R1", "R2", "R3")) / 3
    strangers = sum(dist(f"R{i}") for i in range(4, 12)) / 8
    assert neighbours < strangers


def test_a_global_rail_does_not_make_everything_adjacent():
    """GND touches nearly every part, so counting it makes every part equally
    adjacent to every other and the remaining signal is noise."""
    comps = {f"R{i}": _comp() for i in range(10)}
    gnd_only = {"GND": _net(*[f"R{i}" for i in range(10)])}
    assert placement._adjacency(gnd_only, set(comps)) == {f"R{i}": {} for i in range(10)}


def test_a_hinted_position_is_taken_as_given():
    """A hint is a constraint. Someone wrote it knowing something this does not."""
    comps = {"U1": _comp(), "R1": _comp(), "R2": _comp()}
    r = placement.place(comps, {}, (40, 40), hints={"U1": {"x": 7.5, "y": 12.5}})
    assert (r.placements["U1"].x, r.placements["U1"].y) == (7.5, 12.5)
    assert _overlaps(r) == [], "everything else must work around the hint"


def test_a_near_hint_pulls_a_part_to_its_anchor():
    comps = {"U1": _comp(), "L1": _comp(), **{f"R{i}": _comp() for i in range(8)}}
    nets = {f"N{i}": _net("U1", f"R{i}") for i in range(8)}
    r = placement.place(comps, nets, (60, 60), hints={"L1": {"near": "U1"}})
    u, l = r.placements["U1"], r.placements["L1"]
    far = max(abs(r.placements[f"R{i}"].x - u.x) + abs(r.placements[f"R{i}"].y - u.y)
              for i in range(8))
    assert abs(l.x - u.x) + abs(l.y - u.y) <= far


def test_side_and_rotation_hints_are_carried_through():
    comps = {"U1": _comp()}
    r = placement.place(comps, {}, (40, 40), hints={"U1": {"rot": 90, "side": "bottom"}})
    assert r.placements["U1"].as_dict()["rot"] == 90.0
    assert r.placements["U1"].as_dict()["side"] == "bottom"


def test_a_part_with_no_footprint_geometry_is_given_room_and_noted():
    """An unknown size must crowd its neighbours, never silently overlap them."""
    r = placement.place({"X1": _comp("Nope:NotReal")}, {}, (40, 40))
    assert r.extents["X1"] == placement.FALLBACK_EXTENT
    assert any("X1" in n for n in r.notes)


def test_connection_length_rewards_locality():
    nets = {"N": _net("A", "B")}
    near = {"A": placement.Placement(0, 0), "B": placement.Placement(1, 0)}
    far = {"A": placement.Placement(0, 0), "B": placement.Placement(50, 50)}
    assert placement.total_connection_length(near, nets) < \
           placement.total_connection_length(far, nets)


def test_a_single_pin_net_contributes_no_length():
    """Otherwise a net with one pad would look like a routing cost."""
    assert placement.total_connection_length(
        {"A": placement.Placement(5, 5)}, {"N": _net("A")}
    ) == 0

def test_a_bypass_capacitor_reaches_the_ic_it_is_named_after():
    """The constraint the netlist cannot express, and the wirelength score cannot see.

    A decoupling capacitor sits on a rail and ground and nothing else, so once
    the global rails are excluded from adjacency it has no neighbours at all.
    The first version of this module placed core's C_U1_VDD1 42 mm from U1
    while reporting a 73% wirelength improvement, because that score excludes
    the rails too.
    """
    comps = {"U1": _comp(), "C_U1_VDD1": _comp(), "C_U1_VDD2": _comp()}
    # Eight signal parts that would otherwise take the ring around U1 first.
    comps.update({f"R{i}": _comp() for i in range(8)})
    nets = {"GND": _net("U1", "C_U1_VDD1", "C_U1_VDD2", *[f"R{i}" for i in range(8)])}
    nets.update({f"SIG{i}": _net("U1", f"R{i}") for i in range(8)})

    r = placement.place(comps, nets, (60, 60))
    u = r.placements["U1"]

    def dist(ref):
        p = r.placements[ref]
        return abs(p.x - u.x) + abs(p.y - u.y)

    worst_cap = max(dist("C_U1_VDD1"), dist("C_U1_VDD2"))
    nearest_signal = min(dist(f"R{i}") for i in range(8))
    assert worst_cap <= nearest_signal, (
        "bypass capacitors must get first claim on the space beside their IC"
    )


def test_name_anchoring_prefers_the_longest_matching_refdes():
    """U_CHG must beat a bare U, and a substring match would find one anywhere."""
    refs = {"U", "U_CHG", "C_U_CHG_IN"}
    assert placement._anchor_by_name("C_U_CHG_IN", refs) == "U_CHG"


def test_a_name_that_matches_nothing_is_not_anchored():
    assert placement._anchor_by_name("C_BULK", {"U1", "R1"}) is None


def test_an_explicit_near_hint_beats_the_name():
    """A convention is read; a hint is an instruction."""
    comps = {"U1": _comp(), "U2": _comp(), "C_U1_VDD": _comp(),
             **{f"R{i}": _comp() for i in range(6)}}
    nets = {f"S{i}": _net("U1", f"R{i}") for i in range(6)}
    r = placement.place(comps, nets, (60, 60), hints={"C_U1_VDD": {"near": "U2"}})
    c, u1, u2 = r.placements["C_U1_VDD"], r.placements["U1"], r.placements["U2"]
    assert abs(c.x - u2.x) + abs(c.y - u2.y) <= abs(c.x - u1.x) + abs(c.y - u1.y)


def test_a_circular_courtyard_is_measured_as_a_circle():
    """A circle gives a centre and a point on its circumference, not two corners.

    Reading them as corners collapsed the box to a line: a round test-point
    courtyard measured 1.0 x 0.0 mm, so the part had zero height, every overlap
    test against it passed, and KiCad's DRC then found courtyards crossing on a
    board this module had certified as overlap-free.
    """
    from pathlib import Path

    from blpl.core import symbol_resolution

    roots = [r for r, _ in symbol_resolution.footprint_search_path(
        Path("app/data/projects/example-handheld"), Path("kicad-footprints"))]
    ext = placement.footprint_extent("TestPoint:TestPoint_Pad_D1.0mm", roots)
    assert ext is not None
    assert ext[0] > 0 and ext[1] > 0, "a round courtyard must have both dimensions"
    assert abs(ext[0] - ext[1]) < 1e-6, "a circle's bounding box is square"


def test_a_degenerate_extent_is_refused_rather_than_returned():
    """Zero in either dimension disables the collision test silently."""
    assert placement.footprint_extent("Nope:DoesNotExist", []) is None


def test_a_large_part_is_placed_before_the_passives_scatter_the_space():
    """Space is contiguous only while it is still empty.

    Ordering by connectivity alone left sb-lora's LR2021 module
    (17.1 x 11.0 mm) with nowhere to go on a board that was 44% full, because a
    few dozen 0402s had already been placed around it. The module is the part
    that cannot be squeezed, so it goes first.
    """
    big = (18.0, 12.0)
    small = (1.0, 0.5)

    def fake_box(fp, roots):
        w, h = big if fp == "BIG" else small
        return (w, h, 0.0, 0.0)

    comps = {"U1": _comp("BIG")}
    comps.update({f"C{i}": _comp("SMALL") for i in range(40)})
    # Every passive is tied to the module, so connectivity alone would place
    # them in an order that surrounds it before it exists.
    nets = {f"N{i}": _net("U1", f"C{i}") for i in range(40)}

    import blpl.core.placement as mod

    original = mod.footprint_box
    mod.footprint_box = fake_box
    try:
        r = mod.place(comps, nets, (30, 35))
    finally:
        mod.footprint_box = original

    assert "U1" not in r.unplaced, "the part that cannot be squeezed must be placed first"
    assert r.ok


def test_a_guessed_size_does_not_make_a_part_large():
    """FALLBACK_EXTENT is 5x5 = 25 mm², over the major threshold. Treating an
    unmeasured part as a big one would reorder the board around a guess."""
    r = placement.place({"X1": _comp("Nope:NotReal")}, {}, (40, 40))
    assert "X1" in r.assumed
    assert placement._area(placement.FALLBACK_EXTENT) >= placement.MAJOR_AREA_MM2


def test_an_off_centre_courtyard_is_collided_where_it_actually_is():
    """A footprint's origin is wherever its author put it — pin 1, for most
    connectors. Assuming the courtyard is centred on the origin puts the
    collision box beside the part instead of on it, and KiCad found four such
    overlaps on a board this module had certified as clear."""
    import blpl.core.placement as mod

    def offset_box(fp, roots):
        # 10 x 4 mm body whose origin sits at its left end.
        return (10.0, 4.0, 5.0, 0.0)

    original = mod.footprint_box
    mod.footprint_box = offset_box
    try:
        r = mod.place({"J1": _comp("X"), "J2": _comp("X")}, {}, (40, 40))
    finally:
        mod.footprint_box = original

    assert r.ok
    x0, y0, x1, y1 = mod.occupied_box(r, "J1")
    assert abs((x1 - x0) - 10.0) < 1e-6 and abs((y1 - y0) - 4.0) < 1e-6
    # The box is offset from the stored origin, not centred on it.
    assert abs(((x0 + x1) / 2) - r.placements["J1"].x - 5.0) < 1e-6
    assert _overlaps(r) == []


def test_an_edge_part_does_not_touch_the_board_boundary():
    """Copper hard against the edge fails copper_edge_clearance, and a board
    that cannot be milled is not a routed board."""
    r = placement.place({"J1": _comp(), "J2": _comp(), "J3": _comp()}, {}, (40, 40))
    for ref in ("J1", "J2", "J3"):
        x0, y0, x1, y1 = placement.occupied_box(r, ref)
        assert x0 > 0 and y0 > 0 and x1 < 40 and y1 < 40


def test_a_polygon_courtyard_is_read_not_skipped():
    """Polygon vertices live one level down, in a (pts ...) child.

    Reading only direct children skipped polygon courtyards entirely and
    silently, falling back to the pads: the U.FL jack measured 3.85 x 3.50 mm
    against a real courtyard of 4.35 x 5.00, so three of them were placed 4 mm
    apart and KiCad found all three pairs overlapping.
    """
    from pathlib import Path

    box = placement.footprint_box(
        "Connector_Coaxial:U.FL_Hirose_U.FL-R-SMT-1_Vertical", [Path("kicad-footprints")]
    )
    assert box is not None
    assert box[1] >= 5.0, "the polygon courtyard is 5 mm tall; the pads alone are 3.5"


def test_a_rotated_footprint_is_collided_in_its_rotated_geometry():
    """A ``rot`` hint rotates the emitted footprint; the placer has to reserve
    the rectangle KiCad will actually draw, not the library's unrotated one.

    This is the same failure as the off-centre courtyard above, one step on:
    the box was right in size and place for rot=0 and silently wrong for the
    only case anybody writes a rot hint *for* — an asymmetric edge connector
    turned to face the board edge. Verified against pcbnew: a footprint
    orientation of +90 deg maps a local offset ``(dx, dy)`` to ``(dy, -dx)``.
    """
    import blpl.core.placement as mod

    def wide_box(fp, roots):
        # 12 x 3 mm, origin at the left end — a connector, near enough.
        return (12.0, 3.0, 6.0, 0.0)

    original = mod.footprint_box
    mod.footprint_box = wide_box
    try:
        r = mod.place({"J1": _comp("X")}, {}, (40, 40),
                      hints={"J1": {"x": 20.0, "y": 20.0, "rot": 90}})
    finally:
        mod.footprint_box = original

    x0, y0, x1, y1 = mod.occupied_box(r, "J1")
    # Rotated 90 deg, the 12 mm span is vertical and the 3 mm span horizontal.
    assert abs((x1 - x0) - 3.0) < 1e-6, f"width {x1 - x0} — extents not swapped"
    assert abs((y1 - y0) - 12.0) < 1e-6, f"height {y1 - y0} — extents not swapped"
    # (6, 0) rotates to (0, -6): the body runs *up* from the hinted origin.
    assert abs(((x0 + x1) / 2) - 20.0) < 1e-6
    assert abs(((y0 + y1) / 2) - 14.0) < 1e-6


def test_nothing_is_placed_into_the_space_a_rotated_part_occupies():
    """The consequence that matters.

    A hinted position is taken as given, overlap or not — that is deliberate,
    and two overlapping hints are the human's decision. The bug is the other
    direction: the *reservation* was the unrotated rectangle, so the placer
    left the space the rotated connector really fills marked free and put
    ordinary parts inside it.

    The box for J1 here is computed **by hand** rather than from
    ``occupied_box``. That is the whole point: before the fix the reservation
    and the read-back were wrong in the same direction, so asking the module
    where J1 was would have agreed with the module's own mistake and this test
    would have passed on the broken code. It is only a check if the oracle is
    independent — the same lesson as the placement metric that could not see
    the rails it excluded.
    """
    import blpl.core.placement as mod

    def box_for(fp, roots):
        # The connector is long and asymmetric; everything else is a passive.
        return (12.0, 3.0, 6.0, 0.0) if fp == "CONN" else (2.0, 1.0, 0.0, 0.0)

    original = mod.footprint_box
    mod.footprint_box = box_for
    try:
        comps = {"J1": _comp("CONN")}
        comps.update({f"R{i}": _comp("P") for i in range(60)})
        r = mod.place(comps, {}, (24, 24),
                      hints={"J1": {"x": 12.0, "y": 18.0, "rot": 90}})
    finally:
        mod.footprint_box = original

    # Rotated 90 deg: a 12 x 3 body with its origin at one end becomes a 3 x 12
    # body running up from the origin. Derived from the measured pcbnew
    # mapping (dx, dy) -> (dy, -dx), not from anything under test.
    jx0, jy0, jx1, jy1 = 12.0 - 1.5, 18.0 - 12.0, 12.0 + 1.5, 18.0

    intruders = []
    for ref, p in r.placements.items():
        if ref == "J1":
            continue
        x0, y0, x1, y1 = p.x - 1.0, p.y - 0.5, p.x + 1.0, p.y + 0.5
        if x0 < jx1 - 1e-9 and jx0 < x1 - 1e-9 and y0 < jy1 - 1e-9 and jy0 < y1 - 1e-9:
            intruders.append(ref)

    assert not intruders, (
        f"{len(intruders)} part(s) placed inside the rotated connector's real "
        f"courtyard ({jx0},{jy0})-({jx1},{jy1}): {sorted(intruders)[:6]}"
    )
