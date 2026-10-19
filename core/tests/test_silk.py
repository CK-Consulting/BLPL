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
    assert _props(fp) == {"Reference": True, "Value": True}


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
