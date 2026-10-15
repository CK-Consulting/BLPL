"""Board outlines other than a rectangle, cutouts, and keepouts.

Before this, ``boundaries.board_outline`` was written by ``blpl init``, copied
into the HDM, accepted by the schema with a ``polygon`` option — and read by
nothing. Every board was the rectangle ``project.dimensions`` described, an
edited outline built and routed at the wrong shape, and nothing said so. The
end-to-end test at the bottom is the one that failed then.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from blpl.core import board_geometry as bg
from blpl.core import placement, schema, stage5_emit_yaml_hdm as s5, stage8_review as s8
from blpl.emitter import pcb, sexpr


_FOOTPRINTS_ROOT = Path(__file__).resolve().parent.parent / "kicad-footprints"


def _config(outline: dict | None = None, dims=(50, 30), **boundaries) -> dict:
    b: dict = dict(boundaries)
    if outline is not None:
        b["board_outline"] = outline
    return {"project": {"name": "p", "dimensions": list(dims)}, "boundaries": b}


# --- Shapes ---------------------------------------------------------------------


def test_a_circle_is_measured_from_the_curve_not_its_chords():
    """The flattened polygon sits inside the circle. Measured from it, a 40 mm
    circle started at (0.02, 0.005) and its own dimensions were refused."""
    s = bg.shape({"shape": "circle", "diameter": 40}, "o", default="origin")
    assert s.bbox == pytest.approx((0, 0, 40, 40))


def test_rounding_corners_keeps_a_rectangles_extent():
    s = bg.shape({"shape": "rect", "size": [50, 30], "corner_radius": 4}, "o", default="origin")
    assert s.bbox == pytest.approx((0, 0, 50, 30))
    assert [seg.kind for seg in s.segments].count("arc") == 4
    assert not s.axis_rect


def test_a_rounded_trapezoid_is_moved_back_to_the_origin():
    """Rounding an acute corner pulls the shape in from x = 0; a trapezoid nobody
    positioned is meant to start there."""
    s = bg.shape({"shape": "trapezoid", "bottom": 60, "top": 40, "height": 30,
                  "corner_radius": 2}, "o", default="origin")
    x0, y0, _, y1 = s.bbox
    assert (x0, y0, y1) == pytest.approx((0, 0, 30))


def test_a_trapezoid_puts_its_short_edge_on_top():
    s = bg.shape({"shape": "trapezoid", "bottom": 60, "top": 40, "height": 30}, "o",
                 default="origin")
    top = sorted(p for p in s.polygon if abs(p[1]) < 1e-9)
    bottom = sorted(p for p in s.polygon if abs(p[1] - 30) < 1e-9)
    assert top == pytest.approx([(10, 0), (50, 0)])
    assert bottom == pytest.approx([(0, 30), (60, 30)])


@pytest.mark.parametrize("spec, says", [
    ({"shape": "polygon", "points": [[0, 0], [10, 10], [10, 0], [0, 10]]}, "crosses itself"),
    ({"shape": "polygon", "points": [[0, 0], [10, 0], [20, 0]]}, "encloses no area"),
    ({"shape": "rect", "size": [10, 4], "corner_radius": 3}, "more than half"),
    ({"shape": "polygon", "points": [[0, 0], [10, 0], [10, 2], [0, 2]], "corner_radius": 1.5},
     "too large"),
    ({"shape": "hexagon"}, "must be one of"),
    ({"shape": "circle"}, "no diameter"),
])
def test_a_shape_that_cannot_be_built_is_refused_by_name(spec, says):
    with pytest.raises(bg.BoardGeometryError, match=says):
        bg.shape(spec, "boundaries.board_outline", default="origin")


def test_rotation_follows_the_mapping_measured_with_pcbnew():
    """+90 deg sends a footprint-local (dx, dy) to (dy, -dx) — the same mapping
    placement.rotated_geometry uses, so an anchored shape and its part agree."""
    s = bg.shape({"shape": "rect", "size": [2, 2], "center": [5, 1]}, "k")
    moved = bg.transform(s, 10, 20, 90)
    cx = sum(p[0] for p in moved.polygon) / 4
    cy = sum(p[1] for p in moved.polygon) / 4
    assert (cx, cy) == pytest.approx((10 + 1, 20 - 5))


# --- project.yaml ---------------------------------------------------------------


def test_the_legacy_outline_never_decided_anything_and_still_does_not():
    """``type: rect`` with start/end is what init writes and nothing read. The
    board panel edits dimensions and leaves it stale, so obeying it now would
    resize boards; it is reported instead."""
    board = bg.from_config(_config(
        {"type": "rect", "start": [0, 0], "end": [100, 80], "layer": "Edge.Cuts"}))
    assert board.size == pytest.approx((50, 30))
    assert not board.declared
    assert board.notes and "dimensions decides" in board.notes[0]


def test_a_legacy_polygon_is_refused_rather_than_drawn_as_a_rectangle():
    with pytest.raises(bg.BoardGeometryError, match="shape: polygon"):
        bg.from_config(_config({"type": "polygon"}))


def test_dimensions_must_agree_with_a_shaped_outline():
    with pytest.raises(bg.BoardGeometryError, match=r"set dimensions to \[40, 40\]"):
        bg.from_config(_config({"shape": "circle", "diameter": 40}, dims=(50, 30)))


def test_a_cutout_crossing_the_edge_is_a_notch_and_belongs_in_the_outline():
    with pytest.raises(bg.BoardGeometryError, match="notch"):
        bg.from_config(_config(cutouts=[{"shape": "rect", "center": [0, 15], "size": [6, 6]}]))


def test_a_keepout_forbids_everything_on_every_copper_layer_unless_told():
    board = bg.from_config(_config(keepouts=[{"shape": "rect", "center": [10, 10],
                                              "size": [4, 4]}]))
    (k,) = board.features
    assert k.layers == ("*.Cu",)
    assert set(k.forbid) == set(bg.DEFAULT_FORBID)
    assert k.blocks_parts


def test_a_tracks_only_keepout_does_not_block_parts():
    board = bg.from_config(_config(keepouts=[{"shape": "rect", "center": [10, 10],
                                              "size": [4, 4], "forbid": ["tracks", "vias"]}]))
    assert not board.features[0].blocks_parts


def test_the_schema_names_a_misspelled_keepout_field():
    errors = [e.message for e in schema.validator("project_config").iter_errors(
        _config(keepouts=[{"shape": "rect", "size": [1, 1], "fobid": ["tracks"]}]))]
    assert any("fobid" in m for m in errors)


# --- Placement ------------------------------------------------------------------


def _in_circle(result, r: float) -> list[str]:
    """Independent of the placer's own predicate: every box corner within r."""
    bad = []
    for ref in result.placements:
        x0, y0, x1, y1 = placement.occupied_box(result, ref)
        if any(math.hypot(x - r, y - r) > r + 1e-6 for x in (x0, x1) for y in (y0, y1)):
            bad.append(ref)
    return bad


def test_nothing_lands_outside_a_round_board():
    board = bg.from_config(_config({"shape": "circle", "diameter": 40}, dims=(40, 40)))
    # Thirteen of these fit on it; a square of the same area takes nine.
    comps = {f"R{i}": {} for i in range(12)}
    r = placement.place(comps, {}, board)
    assert r.ok
    assert _in_circle(r, 20) == []


def test_a_round_board_that_is_full_says_so_in_terms_of_its_outline():
    board = bg.from_config(_config({"shape": "circle", "diameter": 12}, dims=(12, 12)))
    r = placement.place({f"R{i}": {} for i in range(12)}, {}, board)
    assert r.unplaced
    assert all("board outline" in why for why in r.unplaced.values())


def test_nothing_lands_in_a_notch():
    notch = [[0, 0], [50, 0], [50, 30], [35, 30], [35, 15], [15, 15], [15, 30], [0, 30]]
    board = bg.from_config(_config({"shape": "polygon", "points": notch}))
    r = placement.place({f"R{i}": {} for i in range(8)}, {}, board)
    assert r.ok
    for ref in r.placements:
        x0, y0, x1, y1 = placement.occupied_box(r, ref)
        assert not (x1 > 15 and x0 < 35 and y1 > 15), ref


def test_parts_stay_out_of_cutouts_and_keepouts():
    board = bg.from_config(_config(
        cutouts=[{"shape": "circle", "center": [25, 15], "diameter": 8}],
        keepouts=[{"shape": "rect", "origin": [0, 0], "size": [50, 8]}],
    ))
    r = placement.place({f"R{i}": {} for i in range(8)}, {}, board)
    assert r.ok
    for ref in r.placements:
        box = placement.occupied_box(r, ref)
        for f in board.features:
            assert not bg.box_overlaps(box, f.shape.polygon), (ref, f.where)


def test_an_anchored_keepout_follows_its_part_and_the_rest_keep_clear():
    """The antenna case: a keepout beside a module, in the module's frame, that
    only exists once the module has a position."""
    board = bg.from_config(_config(
        dims=(40, 40),
        outline={"shape": "rect", "size": [40, 40]},
        keepouts=[{"anchor": "U1", "shape": "rect", "center": [0, -6], "size": [6, 4]}],
    ))
    comps = {"U1": {}, **{f"R{i}": {} for i in range(12)}}
    r = placement.place(comps, {}, board, hints={"U1": {"x": 20, "y": 20}})
    assert r.ok
    (k,) = [f for f in board.features if f.anchor == "U1"]
    xs = [p[0] for p in k.shape.polygon]
    ys = [p[1] for p in k.shape.polygon]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((17, 23, 12, 16))
    for ref in r.placements:
        if ref != "U1":
            assert not bg.box_overlaps(placement.occupied_box(r, ref), k.shape.polygon), ref


def test_a_part_will_not_go_where_its_own_keepout_would_cover_a_neighbour():
    board = bg.from_config(_config(
        dims=(30, 30), outline={"shape": "rect", "size": [30, 30]},
        keepouts=[{"anchor": "U1", "shape": "rect", "center": [0, 0], "size": [12, 12]}],
    ))
    r = placement.place({"U1": {}, "R1": {}}, {}, board, hints={"R1": {"x": 15, "y": 15}})
    assert "U1" in r.placements
    (k,) = [f for f in board.features if f.anchor == "U1"]
    assert not bg.box_overlaps(placement.occupied_box(r, "R1"), k.shape.polygon)


def test_an_edge_hint_puts_the_part_against_that_edge():
    board = bg.from_config(_config(dims=(40, 40), outline={"shape": "rect", "size": [40, 40]}))
    r = placement.place({"U1": {}, "R1": {}}, {}, board, hints={"U1": {"edge": "bottom"}})
    _, _, _, y1 = placement.occupied_box(r, "U1")
    # Within one search step of the bottom edge, and below everything else.
    assert 40 - y1 < 3
    assert y1 > placement.occupied_box(r, "R1")[3]


def test_a_rectangle_from_dimensions_places_exactly_as_before():
    """The fast path: a board whose project.yaml says nothing new must not move."""
    comps = {f"R{i}": {} for i in range(15)}
    nets = {"N1": {"pads": [["R0", "1"], ["R1", "1"]]}}
    old = placement.place(comps, nets, (40, 30))
    new = placement.place(comps, nets, bg.from_config(_config(dims=(40, 30))))
    assert {k: v.as_dict() for k, v in old.placements.items()} == \
           {k: v.as_dict() for k, v in new.placements.items()}


# --- Emitter --------------------------------------------------------------------


def _hdm(config: dict) -> dict:
    board = bg.from_config(config)
    return {
        "project": config["project"],
        "board_geometry": bg.as_record(board),
        "components": {"R1": {"value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric",
                              "placement": {"x": 5, "y": 5, "rot": 0, "side": "top"}}},
        "nets": {},
    }


def _tags(out: str, tag: str) -> list:
    return [c for c in sexpr.parse(out) if isinstance(c, list) and c and c[0] == tag]


def test_arcs_and_circles_are_emitted_as_arcs_and_circles():
    cfg = _config({"shape": "rect", "size": [50, 30], "corner_radius": 4},
                  cutouts=[{"shape": "circle", "center": [25, 15], "diameter": 3.2}])
    out = pcb.emit(_hdm(cfg), footprints_root=_FOOTPRINTS_ROOT)
    assert len(_tags(out, "gr_arc")) == 4
    (circle,) = _tags(out, "gr_circle")
    assert sexpr.find(circle, "center")[1:] == ["37.5", "27.5"]     # (25, 15) + origin
    assert sexpr.find(circle, "end")[1:] == ["39.1", "27.5"]
    assert all(sexpr.find(n, "layer")[1] == '"Edge.Cuts"'
               for tag in ("gr_line", "gr_arc", "gr_circle") for n in _tags(out, tag))


def test_a_keepout_is_emitted_as_a_rule_area():
    cfg = _config(keepouts=[{"shape": "rect", "center": [25, 5], "size": [10, 4],
                             "name": "ANT", "layers": ["F.Cu", "B.Cu"],
                             "forbid": ["tracks", "vias", "copperpour"]}])
    out = pcb.emit(_hdm(cfg), footprints_root=_FOOTPRINTS_ROOT)
    (zone,) = _tags(out, "zone")
    assert sexpr.find(zone, "layers")[1:] == ['"F.Cu"', '"B.Cu"']
    assert sexpr.find(zone, "name")[1] == '"ANT"'
    rules = {r[0]: r[1] for r in sexpr.find(zone, "keepout")[1:]}
    assert rules == {"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed",
                     "copperpour": "not_allowed", "footprints": "allowed"}
    pts = [(float(p[1]), float(p[2])) for p in sexpr.find(sexpr.find(zone, "polygon"), "pts")[1:]]
    assert sorted(pts) == [(32.5, 15.5), (32.5, 19.5), (42.5, 15.5), (42.5, 19.5)]


def test_an_hdm_without_board_geometry_still_gets_its_rectangle():
    hdm = _hdm(_config())
    del hdm["board_geometry"]
    out = pcb.emit(hdm, footprints_root=_FOOTPRINTS_ROOT)
    assert len(_tags(out, "gr_line")) == 4
    assert not _tags(out, "gr_arc")


# --- Stage 5, end to end --------------------------------------------------------


def _stage5_inputs():
    da = {"project_id": "proj", "schema_version": 1, "source_files": [], "components": [],
          "connectors": [], "subsystems": [], "raw_nets": []}
    bom = {"project_id": "proj", "schema_version": 1, "rows": [
        {"local_id": f"R{i}", "refdes": f"R{i}", "mpn": "RC0402", "package": "0402",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric", "symbol_hint": "Device:R",
         "confidence": 0.9} for i in range(4)]}
    nets = {"project_id": "proj", "schema_version": 1, "nets": []}
    return bom, nets, da


def test_a_polygon_outline_in_project_yaml_is_the_board_that_gets_built(tmp_path):
    """The bug: this file used to emit a 60 x 40 rectangle. The outline was
    copied into the HDM and the emitter drew project.dimensions instead."""
    pentagon = [[0, 0], [60, 0], [60, 25], [30, 40], [0, 25]]
    cfg = {
        "project": {"name": "p", "dimensions": [60, 40]},
        "boundaries": {"board_outline": {"shape": "polygon", "points": pentagon}},
        "test_points": {"policy": "none"},
    }
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(cfg))
    config = s5.ensure_project_config(path, "proj")
    hdm, _, _ = s5.emit(*_stage5_inputs(), config)

    out = pcb.emit(hdm, footprints_root=_FOOTPRINTS_ROOT)
    edges = {
        (tuple(float(v) for v in sexpr.find(n, "start")[1:]),
         tuple(float(v) for v in sexpr.find(n, "end")[1:]))
        for n in _tags(out, "gr_line")
    }
    expect = {((pentagon[i][0] + 12.5, pentagon[i][1] + 12.5),
               (pentagon[(i + 1) % 5][0] + 12.5, pentagon[(i + 1) % 5][1] + 12.5))
              for i in range(5)}
    assert edges == expect
    assert hdm["synthesis"]["placement"]["unplaced"] == []


def test_stage5_refuses_dimensions_that_disagree_with_the_outline(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(_config({"shape": "circle", "diameter": 30}, dims=(40, 40))))
    with pytest.raises(s5.InvalidProjectConfigError, match="bounding box"):
        s5.ensure_project_config(path, "proj")


def test_stage5_refuses_a_shape_anchored_to_a_part_that_is_not_there(tmp_path):
    cfg = _config(keepouts=[{"anchor": "U9", "shape": "rect", "size": [2, 2]}])
    cfg["test_points"] = {"policy": "none"}
    with pytest.raises(s5.InvalidProjectConfigError, match="U9"):
        s5.emit(*_stage5_inputs(), cfg)


# --- Stage 8 --------------------------------------------------------------------


def _reviewed_hdm(box: list[float], hinted: bool) -> dict:
    board = bg.from_config(_config(keepouts=[{"shape": "rect", "origin": [0, 0],
                                              "size": [10, 10]}]))
    return {
        "board_geometry": bg.as_record(board),
        "synthesis": {"placement": {"boxes": {"R1": box},
                                    "hinted_positions": ["R1"] if hinted else []}},
    }


def test_stage8_blames_the_placer_for_a_part_it_put_in_a_keepout():
    emitter, design = s8._board_geometry_check(_reviewed_hdm([2, 2, 4, 4], hinted=False))
    assert [c["check"] for c in emitter] == ["placed_in_keepout"]
    assert design == []


def test_stage8_hands_a_hinted_part_in_a_keepout_to_the_design():
    emitter, design = s8._board_geometry_check(_reviewed_hdm([2, 2, 4, 4], hinted=True))
    assert emitter == []
    assert [d["rule_id"] for d in design] == ["placed_in_keepout"]


def test_stage8_finds_a_part_off_a_shaped_board():
    emitter, _ = s8._board_geometry_check(_reviewed_hdm([48, 28, 52, 32], hinted=False))
    assert [c["check"] for c in emitter] == ["placed_off_board"]


def test_stage8_is_quiet_on_a_clean_board():
    assert s8._board_geometry_check(_reviewed_hdm([20, 20, 22, 22], hinted=False)) == ([], [])


def test_stage8_does_not_flag_a_part_against_its_own_anchored_keepout():
    """An antenna keepout overlaps its module on purpose, and the placer allows it."""
    board = bg.from_config(_config(keepouts=[{"anchor": "U1", "shape": "rect",
                                              "center": [0, 0], "size": [6, 6]}]))
    bg.place_anchored(board, "U1", 20, 15, 0)
    hdm = {"board_geometry": bg.as_record(board),
           "synthesis": {"placement": {"boxes": {"U1": [18, 13, 22, 17],
                                                 "R1": [21, 14, 22.5, 15]},
                                       "hinted_positions": []}}}
    emitter, design = s8._board_geometry_check(hdm)
    assert [c["components"] for c in emitter] == [["R1"]]
    assert design == []


def test_a_circle_origin_is_the_top_left_of_its_box():
    """It was accepted by the schema and silently ignored."""
    s = bg.shape({"shape": "circle", "diameter": 4, "origin": [10, 20]}, "k")
    assert s.bbox == pytest.approx((10, 20, 14, 24))


def test_a_polygon_refuses_a_position_beside_its_points():
    with pytest.raises(bg.BoardGeometryError, match="drop origin"):
        bg.shape({"shape": "polygon", "points": [[0, 0], [4, 0], [0, 4]],
                  "origin": [5, 5]}, "k")


def test_an_edge_hint_that_cannot_be_met_leaves_the_part_unplaced():
    """Ranking alone put the part wherever the edge ran out — mid-board, even
    against the opposite edge. A hint is an instruction, so it is refused."""
    board = bg.from_config(_config(
        dims=(40, 40), outline={"shape": "rect", "size": [40, 40]},
        keepouts=[{"shape": "rect", "origin": [0, 30], "size": [40, 10]}],
    ))
    r = placement.place({"U1": {}, "R1": {}}, {}, board, hints={"U1": {"edge": "bottom"}})
    assert "U1" in r.unplaced
    assert "against the bottom edge" in r.unplaced["U1"]
    assert "R1" in r.placements


def test_kicad_happys_keepout_finding_on_a_hinted_part_is_the_designs():
    ctx = s8._Context(pwr_flag_nets=set(), autoroute={}, test_points={},
                      lifecycle_reason=None, emitter_evidence={}, hinted={"J1"})
    finding = {"rule_id": "KO-001", "components": ["J1"]}
    assert s8._classify(finding, ctx) == ("design", None)
    finding = {"rule_id": "KO-001", "components": ["J1", "R4"]}
    assert s8._classify(finding, ctx)[0] == "emitter"


def _zone_bbox(zone) -> tuple[float, float, float, float]:
    pts = [(float(p[1]), float(p[2]))
           for p in sexpr.find(sexpr.find(zone, "polygon"), "pts")[1:]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize("rot, expect", [
    # ESP-WROOM-02's library keepout spans local (-14, -18.1)..(14, -7.1): the
    # antenna end. At (30, 25) that is sheet (28.5, 19.4)..(56.5, 30.4); turned
    # +90 deg, (dx, dy) -> (dy, -dx) puts it on the module's left.
    (0, (28.5, 19.4, 56.5, 30.4)),
    (90, (24.4, 23.5, 35.4, 51.5)),
])
def test_a_library_footprints_own_keepout_lands_on_the_footprint(rot, expect):
    """A board file stores a footprint's zones in board coordinates; copying the
    library's local ones put this antenna keepout off the sheet at (-14, -18)."""
    hdm = {"project": {"dimensions": [60, 50]}, "nets": {},
           "components": {"U1": {"value": "esp", "footprint": "RF_Module:ESP-WROOM-02",
                                 "placement": {"x": 30, "y": 25, "rot": rot, "side": "top"}}}}
    fp = sexpr.find(sexpr.parse(pcb.emit(hdm, footprints_root=_FOOTPRINTS_ROOT)), "footprint")
    (zone,) = [c for c in fp if isinstance(c, list) and c and c[0] == "zone"]
    assert _zone_bbox(zone) == pytest.approx(expect, abs=0.06)


def test_an_anchored_keepout_belongs_to_its_footprint_not_the_board():
    """KiCad exempts only a rule area's own footprint from it: drawn at board
    level, an antenna keepout reported its own module as not allowed."""
    cfg = _config(dims=(60, 50), outline={"shape": "rect", "size": [60, 50]},
                  keepouts=[{"anchor": "R1", "shape": "rect", "center": [0, -4],
                             "size": [4, 2], "name": "ANT"}])
    board = bg.from_config(cfg)
    bg.place_anchored(board, "R1", 5, 5, 0)
    hdm = {"project": cfg["project"], "board_geometry": bg.as_record(board), "nets": {},
           "components": {"R1": {"value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric",
                                 "placement": {"x": 5, "y": 5, "rot": 0, "side": "top"}}}}
    out = pcb.emit(hdm, footprints_root=_FOOTPRINTS_ROOT)
    assert _tags(out, "zone") == []
    fp = sexpr.find(sexpr.parse(out), "footprint")
    (zone,) = [c for c in fp if isinstance(c, list) and c and c[0] == "zone"]
    assert sexpr.find(zone, "name")[1] == '"ANT"'
    assert _zone_bbox(zone) == pytest.approx((15.5, 12.5, 19.5, 14.5))
