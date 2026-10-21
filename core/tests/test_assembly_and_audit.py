"""Parts not simply bought and placed, and every placed part checked against the board."""

from __future__ import annotations

import pytest

from blpl.core import board_geometry, placement, release, stage5_emit_yaml_hdm
from blpl.emitter import pcb, sch, sexpr


def _comps(*refs):
    return {r: {"footprint": "", "value": r} for r in refs}


def test_assembly_marks_parts_and_refuses_unknown_or_doubled_ones():
    comps = _comps("J_DBG", "R1", "C1")
    stage5_emit_yaml_hdm._apply_assembly(comps, {"assembly": {"footprint_only": ["J_DBG"], "dnp": ["R1"]}})
    assert comps["J_DBG"]["assembly"] == "footprint_only" and comps["R1"]["assembly"] == "dnp"
    assert "assembly" not in comps["C1"]
    with pytest.raises(stage5_emit_yaml_hdm.InvalidProjectConfigError, match="not a part"):
        stage5_emit_yaml_hdm._apply_assembly(_comps("R1"), {"assembly": {"dnp": ["R9"]}})
    with pytest.raises(stage5_emit_yaml_hdm.InvalidProjectConfigError, match="both"):
        stage5_emit_yaml_hdm._apply_assembly(_comps("R1"), {"assembly": {"dnp": ["R1"], "footprint_only": ["R1"]}})


def test_the_schematic_and_board_carry_kicads_own_flags():
    def flags(node, *names):
        return {n: sexpr.find(node, n)[1] for n in names}

    dnp = sch._component_instance("R1", "Device:R", "1k", "", 0, 0, "p", '"u"', assembly="dnp")
    assert flags(dnp, "in_bom", "in_pos_files", "dnp", "on_board") == {
        "in_bom": "no", "in_pos_files": "no", "dnp": "yes", "on_board": "yes"}
    only = sch._component_instance("J_DBG", "Device:R", "x", "", 0, 0, "p", '"u"', assembly="footprint_only")
    assert flags(only, "in_bom", "dnp") == {"in_bom": "no", "dnp": "no"}
    fitted = sch._component_instance("C1", "Device:C", "1u", "", 0, 0, "p", '"u"')
    assert flags(fitted, "in_bom", "in_pos_files", "dnp") == {"in_bom": "yes", "in_pos_files": "yes", "dnp": "no"}

    fp = ["footprint", '"X"', ["attr", "smd"]]
    pcb._set_assembly(fp, "dnp")
    assert sexpr.find(fp, "attr")[1:] == ["smd", "exclude_from_pos_files", "exclude_from_bom", "dnp"]
    fp2 = ["footprint", '"X"']
    pcb._set_assembly(fp2, "footprint_only")
    assert sexpr.find(fp2, "attr")[1:] == ["exclude_from_pos_files", "exclude_from_bom"]


def test_the_board_carries_the_part_number_hidden_as_the_schematic_does():
    fp = ["footprint", '"X"', ["property", '"Reference"', '"R1"'],
          ["property", '"MPN"', '"OLD"']]
    pcb._set_part_fields(fp, {"mpn": "RC0402FR-07470RL", "manufacturer": "Yageo"})
    props = {sexpr.unquote(p[1]): p for p in sexpr.find_all(fp, "property")}
    assert sexpr.unquote(props["MPN"][2]) == "RC0402FR-07470RL"
    assert sexpr.unquote(props["Manufacturer"][2]) == "Yageo"
    assert sexpr.find(props["Manufacturer"], "hide")[1] == "yes"
    assert len(props) == 3  # the stale MPN was updated, not doubled

    bare = ["footprint", '"TP"']
    pcb._set_part_fields(bare, {"mpn": "", "manufacturer": None})
    assert sexpr.find_all(bare, "property") == []


def test_the_release_bom_leaves_out_parts_that_are_not_bought(tmp_path):
    import csv
    import json

    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps({"rows": [
        {"local_id": "J_DBG", "mpn": "TC2030-NL-FP"}, {"local_id": "C1", "mpn": "GRM"}]}))
    out = tmp_path / "bom.csv"
    release.export_bom_csv(bom, out, exclude={"J_DBG"})
    rows = list(csv.DictReader(out.open()))
    assert [r["MPN"] for r in rows] == ["GRM"]


def _result(boxes):
    r = placement.Result()
    for ref, (cx, cy, w, h) in boxes.items():
        r.placements[ref] = placement.Placement(cx, cy, 0.0, "top")
        r.extents[ref] = (w, h)
        r.offsets[ref] = (0.0, 0.0)
    return r


def test_pinned_parts_are_audited_against_the_board():
    board = board_geometry.from_config({
        "project": {"name": "t", "dimensions": [40, 30]},
        "boundaries": {"keepouts": [{"shape": "rect", "origin": [30, 0], "size": [10, 10],
                                     "name": "antenna", "forbid": ["footprints"]}]},
    })
    result = _result({
        "U1": (10, 10, 6, 6),     # fine
        "C1": (1.2, 15, 1, 1),    # 0.7 mm from the left edge: inside a 1 mm margin
        "R1": (39.8, 20, 2, 1),   # crosses the right edge
        "L1": (35, 5, 2, 2),      # in the antenna keepout
        "C2": (11, 11, 2, 2),     # on top of U1
    })
    hints = {r: {"x": 0, "y": 0} for r in ("C1", "R1", "L1", "C2")}
    found = {(f["ref"], f["problem"]) for f in placement.audit(result, board, hints, 1.0)}
    assert ("R1", "off_board") in found
    assert ("C1", "in_edge_margin") in found
    assert ("L1", "in_keepout") in found
    assert ("U1", "overlap") in found or ("C2", "overlap") in found
    assert not any(ref == "U1" and p != "overlap" for ref, p in found)
    assert all(f["pinned"] for f in placement.audit(result, board, hints, 1.0) if f["ref"] != "U1")


def test_a_librarys_dnp_flag_never_unfits_a_part():
    # Seeed's Wio-LR2021 footprint ships with (attr smd dnp): the radio was
    # left out of the placement file and BOM of every board that used it.
    fp = ["footprint", '"Seeed:Wio-LR2021_V1"', ["attr", "smd", "dnp"]]
    pcb._set_assembly(fp, None)
    assert sexpr.find(fp, "attr")[1:] == ["smd"]
    tp = ["footprint", '"TestPoint"', ["attr", "exclude_from_pos_files", "exclude_from_bom"]]
    pcb._set_assembly(tp, None)  # a library's BOM exclusion for a test pad stands
    assert sexpr.find(tp, "attr")[1:] == ["exclude_from_pos_files", "exclude_from_bom"]


def test_cutouts_are_board_edge_to_the_audit():
    board = board_geometry.from_config({
        "project": {"name": "t", "dimensions": [40, 30]},
        "boundaries": {"cutouts": [{"shape": "rect", "center": [20, 15], "size": [6, 6]}]},
    })
    result = _result({
        "U1": (20, 15, 2, 2),     # in the cutout (17..23)
        "C1": (23.9, 15, 1, 1),   # 0.4 mm beside it
        "R1": (8, 8, 1, 1),       # fine
    })
    found = {(f["ref"], f["problem"]) for f in placement.audit(result, board, {}, 1.0)}
    assert found == {("U1", "in_cutout"), ("C1", "in_edge_margin")}


def test_a_part_on_the_back_is_judged_by_the_back_sides_rules():
    """The ear tag's debug pads, moved under the cell on B.Cu, were reported as
    inside the pocket's F.Cu-only footprint keepouts and on the parts above."""
    board = board_geometry.from_config({
        "project": {"name": "t", "dimensions": [40, 30]},
        "boundaries": {"keepouts": [
            {"name": "front only", "shape": "rect", "center": [10, 15], "size": [8, 8],
             "layers": ["F.Cu"], "forbid": ["footprints"]},
            {"name": "both", "shape": "rect", "center": [30, 15], "size": [8, 8],
             "layers": ["*.Cu"], "forbid": ["footprints"]},
            {"name": "outer pair", "shape": "rect", "center": [30, 25], "size": [6, 6],
             "layers": ["F&B.Cu"], "forbid": ["footprints"]},
        ]},
    })
    result = _result({
        "J1": (10, 15, 2, 2),   # under the front-only keepout, on the back
        "J2": (30, 15, 2, 2),   # under the both-sides keepout, on the back
        "R1": (20, 5, 2, 2),    # on the front
        "J3": (20, 5, 2, 2),    # directly under R1, on the back
        "J4": (30, 25, 2, 2),   # under the F&B.Cu keepout, on the back
        "R2": (31, 24, 1, 1),   # under the same keepout, on the front
    })
    for ref in ("J1", "J2", "J3", "J4"):
        result.placements[ref].side = "bottom"
    found = {(f["ref"], f["problem"]) for f in placement.audit(result, board, {}, 1.0)}
    assert found == {("J2", "in_keepout"), ("J4", "in_keepout"), ("R2", "in_keepout")}

    result.placements["J1"].side = "top"
    found = {(f["ref"], f["problem"]) for f in placement.audit(result, board, {}, 1.0)}
    assert ("J1", "in_keepout") in found


def test_edge_clearance_is_measured_along_the_courtyard_not_at_its_corners():
    # A V notch whose tip comes to 0.5 mm of the middle of U1's top edge,
    # while every corner of U1 is several millimetres clear.
    board = board_geometry.from_config({
        "project": {"name": "t", "dimensions": [40, 30]},
        "boundaries": {"board_outline": {"shape": "polygon", "points": [
            [0, 0], [40, 0], [40, 30], [21, 30], [20, 20.5], [19, 30], [0, 30]]}},
    })
    result = _result({"U1": (20, 15, 10, 10)})
    found = placement.audit(result, board, {}, 1.0)
    assert [(f["ref"], f["problem"]) for f in found] == [("U1", "in_edge_margin")]
    assert "0.50 mm" in found[0]["detail"]


@pytest.mark.parametrize("text", ["- a\n- b\n", "components: [1, 2]\n", "just text\n"])
def test_a_malformed_hdm_leaves_the_bom_whole_instead_of_crashing(tmp_path, text):
    (tmp_path / "hdm.yaml").write_text(text)
    assert release._not_bought(tmp_path) == set()
