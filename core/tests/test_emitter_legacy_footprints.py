"""Pre-v10 footprints embedded in a v10 board.

Vendor libraries arrive in whatever format the part's maker exported: KiCad 5
``(module ...)`` files, KiCad 6 files with ``(fp_text reference "REF**")`` and
``(width w)`` strokes. KiCad upgrades those when it loads a *library*, not when
it meets them inside a board, so an emitter that copies them in verbatim
produces a board kicad-cli refuses ("Expecting 'mid'") and footprints whose
reference is still ``REF**``. These tests pin the pure-Python upgrade.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from blpl.emitter import pcb, sexpr


_FOOTPRINTS_ROOT = Path(__file__).resolve().parent.parent / "kicad-footprints"

# A trimmed copy of the InvenSense ICM42670P footprint as SnapEDA shipped it:
# KiCad 5 head, fp_text reference/value, legacy arcs and widths.
_LEGACY = """
(module "ICM42670P" (layer F.Cu) (tedit 5F1234AB)
  (descr "14 Lead LGA (2.5x3x0.76)")
  (attr smd)
  (fp_text reference IC** (at -0.225 -0) (layer F.SilkS)
    (effects (font (size 1.27 1.27) (thickness 0.254)))
  )
  (fp_text user %R (at -0.225 -0) (layer F.Fab)
    (effects (font (size 1.27 1.27) (thickness 0.254)))
  )
  (fp_text value "ICM42670P" (at -0.225 -0) (layer F.SilkS) hide
    (effects (font (size 1.27 1.27) (thickness 0.254)))
  )
  (fp_line (start -1.5 -1.25) (end 1.5 -1.25) (layer F.Fab) (width 0.2))
  (fp_arc (start -1.9 -0.75) (end -1.900 -0.8) (angle -180) (layer F.SilkS) (width 0.1))
  (fp_arc (start -1.9 -0.75) (end -1.900 -0.7) (angle -180) (layer F.SilkS) (width 0.1))
  (pad 1 smd rect (at -1.150 -0.75 90) (size 0.300 0.600) (layers F.Cu F.Paste F.Mask))
  (pad 2 smd rect (at -1.150 -0.25 90) (size 0.300 0.600) (layers F.Cu F.Paste F.Mask))
)
"""


def _upgraded() -> sexpr.Node:
    node = sexpr.parse(_LEGACY)
    pcb._upgrade_legacy_footprint(node)
    return node


def _pt(node: sexpr.Node, tag: str) -> tuple[float, float]:
    n = sexpr.find(node, tag)
    return (float(n[1]), float(n[2]))


def test_module_becomes_footprint_and_tedit_is_dropped() -> None:
    node = _upgraded()
    assert node[0] == "footprint"
    assert sexpr.find(node, "tedit") is None


def test_reference_and_value_become_properties_that_the_emitter_can_patch() -> None:
    node = _upgraded()
    # No fp_text reference/value survives — only the user text does.
    kinds = [t[1] for t in sexpr.find_all(node, "fp_text")]
    assert kinds == ["user"]
    props = {sexpr.unquote(p[1]): p for p in sexpr.find_all(node, "property")}
    assert set(props) >= {"Reference", "Value"}
    # `hide` on the value text became the node form KiCad 10 writes.
    assert sexpr.find(props["Value"], "hide") == ["hide", "yes"]
    # And the existing patcher now reaches them — this is the REF** fix.
    pcb._set_reference_and_value(node, "U_IMU", "ICM-42670-P")
    assert sexpr.unquote(props["Reference"][2]) == "U_IMU"
    assert sexpr.unquote(props["Value"][2]) == "ICM-42670-P"


def test_legacy_arcs_get_a_mid_point_on_the_right_side() -> None:
    node = _upgraded()
    arcs = sexpr.find_all(node, "fp_arc")
    assert len(arcs) == 2
    for arc in arcs:
        assert sexpr.find(arc, "angle") is None
        assert sexpr.find(arc, "mid") is not None
    # The two 180° arcs share a centre and together draw one circle, so their
    # mid points must sit diametrically opposite each other across it.
    cx, cy = -1.9, -0.75
    (m1x, m1y), (m2x, m2y) = (_pt(arcs[0], "mid"), _pt(arcs[1], "mid"))
    assert abs((m1x + m2x) / 2 - cx) < 1e-6 and abs((m1y + m2y) / 2 - cy) < 1e-6
    # ...and each lies on the circle at the radius the legacy end point set.
    r = 0.05
    for mx, my in ((m1x, m1y), (m2x, m2y)):
        assert abs(((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5 - r) < 1e-6
    # A negative sweep swaps the ends, so start and end are the two legacy
    # start points exchanged: the arc keeps its two endpoints either way.
    ends = {_pt(arcs[0], "start"), _pt(arcs[0], "end")}
    assert ends == {(-1.9, -0.8), (-1.9, -0.7)}


def test_legacy_widths_become_strokes() -> None:
    node = _upgraded()
    for tag in ("fp_line", "fp_arc"):
        for shape in sexpr.find_all(node, tag):
            assert sexpr.find(shape, "width") is None
            stroke = sexpr.find(shape, "stroke")
            assert stroke is not None and sexpr.find(stroke, "width") is not None


def test_upgrade_is_a_no_op_on_a_modern_footprint() -> None:
    ref = "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal"
    from blpl.emitter import loaders

    node = deepcopy(loaders.load_footprint(ref, _FOOTPRINTS_ROOT))
    before = sexpr.dump(node)
    pcb._upgrade_legacy_footprint(node)
    assert sexpr.dump(node) == before


def test_emitted_board_carries_the_refdes_for_a_legacy_footprint(tmp_path: Path) -> None:
    lib = tmp_path / "InvenSense.pretty"
    lib.mkdir()
    (lib / "ICM42670P.kicad_mod").write_text(_LEGACY, encoding="utf-8")
    hdm = {
        "project": {"name": "Legacy", "dimensions": [30, 30], "stackup": {"thickness": 1.6}},
        "components": {
            "U_IMU": {
                "value": "ICM-42670-P",
                "footprint": "InvenSense:ICM42670P",
                "placement": {"x": 5, "y": 5},
                "pin_map": {"GND": "1"},
            }
        },
        "nets": {"GND": {"class": "Power_Bulk", "pads": [["U_IMU", "GND"]]}},
    }
    out = pcb.emit(hdm, footprints_root=[tmp_path, _FOOTPRINTS_ROOT])
    assert "(module" not in out
    assert "REF**" not in out and "IC**" not in out
    assert '(property "Reference" "U_IMU"' in out
    assert "(angle" not in out and "(mid" in out
