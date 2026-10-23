"""project.yaml silkscreen policy, applied per footprint (blpl.emitter.silk)."""

from __future__ import annotations

import copy

import pytest

from blpl.emitter import sexpr, silk

FP = sexpr.parse('''(footprint "X"
  (property "Reference" "J_RF_GNSS" (at 0 -2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
  (property "Value" "U.FL" (at 0 2 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
  (fp_line (start -3 0) (end 3 0) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "l1"))
  (fp_line (start -3 3) (end 3 3) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "l2"))
  (fp_circle (center 0 0) (end 0.3 0) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "c1"))
  (fp_line (start -3 0) (end 3 0) (stroke (width 0.1) (type solid)) (layer "F.Fab") (uuid "f1"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))
)''')


def _fp():
    return copy.deepcopy(FP)


def _props(fp):
    return {sexpr.unquote(p[1]): any(sexpr.head(c) == "hide" for c in p if isinstance(c, list))
            for p in sexpr.find_all(fp, "property")}


def test_no_policy_changes_nothing():
    fp = _fp()
    silk.apply(fp, policy=None, label=None)
    assert fp == FP


def test_hidden_references_leave_the_silk():
    fp = _fp()
    silk.apply(fp, policy={"references": "hidden"}, label=None)
    # Only the references: a Value someone put in silk stays.
    assert _props(fp) == {"Reference": True, "Value": False}


def test_a_label_takes_the_references_place_in_silk():
    fp = _fp()
    silk.apply(fp, policy={"label_size": 0.8}, label="GNSS")
    (text,) = sexpr.find_all(fp, "fp_text")
    assert sexpr.unquote(text[2]) == "GNSS" and sexpr.find(text, "at")[1:3] == ["0", "-2"]
    assert sexpr.unquote(sexpr.find(text, "layer")[1]) == "F.SilkS"
    assert _props(fp)["Reference"] is True and _props(fp)["Value"] is False


def test_outlines_are_trimmed_off_the_pads_and_the_rest_kept():
    fp = _fp()
    silk.apply(fp, policy={"clip_to_copper": True}, label=None)
    silk_lines = [n for n in sexpr.find_all(fp, "fp_line") if sexpr.unquote(sexpr.find(n, "layer")[1]) == "F.SilkS"]
    # The line through the pad becomes two pieces either side of it; the one
    # clear of it is untouched; the circle drawn on the pad is gone; F.Fab stays.
    spans = sorted((float(sexpr.find(n, "start")[1]), float(sexpr.find(n, "end")[1]))
                   for n in silk_lines if float(sexpr.find(n, "start")[2]) == 0)
    assert len(spans) == 2 and spans[0][0] == -3 and spans[1][1] == 3
    assert all(abs(x) > 0.5 + silk.PAD_MARGIN for span in spans for x in span if abs(x) < 3)
    assert any(float(sexpr.find(n, "start")[2]) == 3 for n in silk_lines)
    assert not sexpr.find_all(fp, "fp_circle")
    assert any(sexpr.unquote(sexpr.find(n, "layer")[1]) == "F.Fab" for n in sexpr.find_all(fp, "fp_line"))


def _clip(body: str) -> list:
    fp = sexpr.parse(f'(footprint "X" {body})')
    silk.clip_to_copper(fp)
    return fp


def test_a_rotated_pad_is_tested_in_kicads_frame():
    # KiCad rotates counter-clockwise on screen with Y down: a 2 x 0.4 pad at
    # 30 degrees covers (0.78, -0.45), checked against pcbnew's HitTest.
    pads = silk._pads(sexpr.parse('(footprint "X" (pad "1" smd rect (at 0 0 30) (size 2 0.4) (layers "F.Cu")))'), "F")
    assert silk._on_pad(0.78, -0.45, pads, 0) and not silk._on_pad(0.78, 0.45, pads, 0)


def test_an_arc_clear_of_a_pad_stays_when_its_circle_would_not():
    # A quarter arc on the right; the pad sits on the left of the same circle.
    arc = '(fp_arc (start 1 -1) (mid 1.4142 0) (end 1 1) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))'
    fp = _clip(arc + ' (pad "1" smd rect (at -1.4142 0) (size 0.5 0.5) (layers "F.Cu"))')
    assert sexpr.find_all(fp, "fp_arc")
    fp = _clip(arc + ' (pad "1" smd rect (at 1.4142 0) (size 0.5 0.5) (layers "F.Cu"))')
    assert not sexpr.find_all(fp, "fp_arc")


def test_a_custom_pads_primitives_are_copper_too():
    # FlipProbe's pad 5: a 0.4 mm anchor, a polygon out to 0.6 mm.
    pad = ('(pad "5" smd custom (at 0 0) (size 0.4 0.4) (layers "F.Cu") (options (clearance outline) (anchor rect))'
           ' (primitives (gr_poly (pts (xy 0 -0.1) (xy 1.5 -0.1) (xy 1.5 0.1) (xy 0 0.1)) (width 0) (fill yes))))')
    fp = _clip('(fp_line (start 1.2 -2) (end 1.2 2) (stroke (width 0.12) (type solid)) (layer "F.SilkS")) ' + pad)
    assert len(sexpr.find_all(fp, "fp_line")) == 2


def test_a_filled_graphic_over_a_pad_goes_though_its_outline_is_clear():
    pad = '(pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))'
    filled = '(fp_rect (start -2 -2) (end 2 2) (stroke (width 0.12) (type solid)) (fill yes) (layer "F.SilkS"))'
    assert not sexpr.find_all(_clip(filled + " " + pad), "fp_rect")
    assert sexpr.find_all(_clip(filled.replace("(fill yes)", "(fill no)") + " " + pad), "fp_rect")


def test_an_unknown_labelled_part_is_refused():
    from blpl.core import stage5_emit_yaml_hdm as s5

    with pytest.raises(s5.InvalidProjectConfigError, match="J_NOPE"):
        s5._apply_silkscreen({}, {"U1": {}}, {"silkscreen": {"labels": {"J_NOPE": "X"}}})


def test_the_schema_takes_the_policy():
    from blpl.core import schema

    base = {"project": {"name": "p", "dimensions": [10, 10]}}
    schema.validate("project_config", {**base, "silkscreen": {
        "references": "hidden", "labels": {"BT_POS": "+"}, "label_size": 0.8, "clip_to_copper": True}})
    with pytest.raises(Exception):
        schema.validate("project_config", {**base, "silkscreen": {"label_size": 0.5}})
    schema.validate("project_config", {**base, "silkscreen": {"marking": {"text": "VQT0P1 REV 0", "at": [44, 46]}}})
    with pytest.raises(Exception):
        schema.validate("project_config", {**base, "silkscreen": {"marking": {"text": "VQT0P1"}}})


def test_the_board_marking_is_board_text_on_the_back_mirrored():
    """Board-level text is what a fab's check reads for a name and revision;
    every other silk text BLPL wrote belonged to a part."""
    node = silk.marking({"marking": {"text": "VQT0P1 REV 0", "at": [44, 46]}}, origin=(12.5, 12.5))
    assert sexpr.head(node) == "gr_text" and sexpr.unquote(node[1]) == "VQT0P1 REV 0"
    assert sexpr.find(node, "at")[1:] == ["56.500", "58.500", "0"]
    assert sexpr.unquote(sexpr.find(node, "layer")[1]) == "B.SilkS"
    assert sexpr.find(sexpr.find(node, "effects"), "justify") == ["justify", "mirror"]

    front = silk.marking({"marking": {"text": "X1", "at": [0, 0], "side": "front", "size": 1.5}}, origin=(0, 0))
    assert sexpr.unquote(sexpr.find(front, "layer")[1]) == "F.SilkS"
    assert sexpr.find(sexpr.find(front, "effects"), "justify") is None
    assert silk.marking({"references": "hidden"}, origin=(0, 0)) is None


def test_an_outline_is_trimmed_off_a_neighbours_pads_once_both_are_placed():
    """clip_to_copper saw only a part's own pads, so U1's outline on the ear
    tag stayed printed across C1's and C2's: seven silk_over_copper warnings."""
    # A sits at (10, 10) turned 90; its local (x, 2) is board (12, 10 - x)...
    a = sexpr.parse('''(footprint "A" (layer "F.Cu") (at 10 10 90)
      (fp_line (start -3 2) (end 3 2) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "a1"))
      (fp_line (start -3 -2) (end 3 -2) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "a2")))''')
    # ...so B's pad at board (12, 10) lies on A's first line, and only there.
    b = sexpr.parse('''(footprint "B" (layer "F.Cu") (at 12 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))''')
    # A pad on the back does not trim front silk.
    c = sexpr.parse('''(footprint "C" (layer "B.Cu") (at 8 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu" "B.Mask")))''')
    assert silk.clip_to_neighbours([a, b, c]) == 1
    lines = [(float(sexpr.find(n, "start")[1]), float(sexpr.find(n, "end")[1]), float(sexpr.find(n, "start")[2]))
             for n in sexpr.find_all(a, "fp_line")]
    on_b = sorted(l[:2] for l in lines if l[2] == 2)
    assert len(on_b) == 2 and on_b[0][0] == -3 and on_b[1][1] == 3
    assert all(abs(x) > 0.5 + silk.PAD_MARGIN for span in on_b for x in span if abs(x) < 3)
    assert (-3.0, 3.0, -2.0) in lines


def test_a_long_neighbouring_pad_with_no_corner_near_the_silk_still_trims_it():
    """A 10 x 1 mm pad crossing a short silk line has every corner outside the
    line's box; selected by corners, it was skipped (Codex, PR #72)."""
    a = sexpr.parse('''(footprint "A" (layer "F.Cu") (at 10 10)
      (fp_line (start 0 -1) (end 0 1) (stroke (width 0.12) (type solid)) (layer "F.SilkS") (uuid "a1")))''')
    b = sexpr.parse('''(footprint "B" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 10 1) (layers "F.Cu" "F.Mask")))''')
    assert silk.clip_to_neighbours([a, b]) == 1
    for n in sexpr.find_all(a, "fp_line"):
        ys = (float(sexpr.find(n, "start")[2]), float(sexpr.find(n, "end")[2]))
        assert all(abs(y) > 0.5 for y in ys)
