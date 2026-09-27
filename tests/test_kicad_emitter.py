"""Unit tests for pipeline.kicad_emitter (sexpr, loaders, pcb, sch, pro)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blpl.emitter import loaders, pcb, pro, sch, sexpr


_SYMBOLS_ROOT = Path(__file__).resolve().parent.parent / "kicad-symbols"
_FOOTPRINTS_ROOT = Path(__file__).resolve().parent.parent / "kicad-footprints"


def _minimal_hdm() -> dict:
    return {
        "project": {
            "name": "Emitter Test",
            "board_id": "ET-1",
            "dimensions": [50, 40],
            "stackup": {"layers": 2, "thickness": 1.6, "finish": "HASL"},
        },
        "net_classes": {
            "Default": {"trace_width": 0.2, "clearance": 0.15, "via_dia": 0.6, "via_drill": 0.3}
        },
        "components": {
            "J1": {
                "value": "PJ-063AH",
                "footprint": "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
                "lib_symbol": "Connector:Barrel_Jack",
                "placement": {"x": 20, "y": 20, "rot": 0.0, "side": "top"},
                "pin_map": {"VIN": "1", "GND": "2"},
            }
        },
        "nets": {
            "VIN": {"class": "Default", "pads": [["J1", "VIN"]]},
            "GND": {"class": "Default", "pads": [["J1", "GND"]]},
        },
    }


# ---------------------------------------------------------------------------
# sexpr
# ---------------------------------------------------------------------------


def test_sexpr_roundtrip_simple() -> None:
    text = '(foo "bar baz" 1 2 (nested "x"))'
    node = sexpr.parse(text)
    rendered = sexpr.dump(node)
    # Output is normalized (tabs, newlines) but re-parses to the same structure.
    assert sexpr.parse(rendered) == node


def test_sexpr_find_and_find_all() -> None:
    node = sexpr.parse("(root (a 1) (a 2) (b 3))")
    assert sexpr.head(sexpr.find(node, "a")) == "a"
    assert len(sexpr.find_all(node, "a")) == 2
    assert sexpr.find(node, "missing") is None


def test_sexpr_quote_roundtrip() -> None:
    assert sexpr.unquote(sexpr.quote("hello")) == "hello"
    assert sexpr.unquote(sexpr.quote('a "quoted" b')) == 'a "quoted" b'


# ---------------------------------------------------------------------------
# loaders (exercise against the real submodule)
# ---------------------------------------------------------------------------


def test_load_footprint_from_library() -> None:
    fp = loaders.load_footprint(
        "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal", _FOOTPRINTS_ROOT
    )
    assert sexpr.head(fp) == "footprint"
    pads = loaders.extract_pads(fp)
    assert len(pads) >= 2  # barrel jacks have at least VIN+GND


def test_load_footprint_raises_on_miss() -> None:
    with pytest.raises(loaders.LibraryMiss):
        loaders.load_footprint("Nonexistent_Lib:Nonexistent_Name", _FOOTPRINTS_ROOT)


def test_lib_symbols_carry_the_whole_symbol_including_pins() -> None:
    """lib_symbols is a cache, not a stub — and KiCad reads pin positions from it.

    This used to emit a "shell" with the sub-symbols stripped, believing KiCad
    re-resolved geometry from the installed library. It does not. A symbol with no
    sub-symbols is a symbol with NO PINS, so nothing on the sheet could connect to
    anything: dev.04 came out with 134 wires dangling in mid-air and every net
    label unattached, while looking completely normal.
    """
    sym = loaders.load_symbol_def("Connector:Barrel_Jack", _SYMBOLS_ROOT)
    assert sexpr.unquote(sym[1]) == "Connector:Barrel_Jack"

    nested = [c for c in sym if isinstance(c, list) and sexpr.head(c) == "symbol"]
    assert nested, "sub-symbols (pins + body) must be carried through"
    pins = [p for unit in nested for p in sexpr.find_all(unit, "pin")]
    assert pins, "a symbol with no pins cannot connect to anything"

    # Library default property values are kept; the instance overrides what it needs.
    props = sexpr.find_all(sym, "property")
    assert any(sexpr.unquote(str(pr[2])) for pr in props if len(pr) >= 3)


def test_pcb_emit_has_v10_header() -> None:
    out = pcb.emit(_minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT)
    assert out.startswith("(kicad_pcb")
    assert "(version 20260206)" in out
    assert '(generator_version "10.0")' in out


def test_pcb_emit_includes_nets_and_footprint() -> None:
    out = pcb.emit(_minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT)
    assert '(net 0 "")' in out
    assert '(net 1 "VIN")' in out
    assert '(net 2 "GND")' in out
    assert '(footprint "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal"' in out


def test_pcb_pad_net_override_via_pin_map() -> None:
    out = pcb.emit(_minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT)
    # The pin_map resolves VIN→pad "1" and GND→pad "2".
    # Parse back and verify the pads carry the right net references.
    node = sexpr.parse(out)
    footprint = sexpr.find(node, "footprint")
    pads = loaders.extract_pads(footprint)
    pad_nets: dict[str, str] = {}
    for pad in pads:
        pad_num = sexpr.unquote(pad[1])
        net_node = sexpr.find(pad, "net")
        if net_node is not None:
            pad_nets[pad_num] = sexpr.unquote(net_node[2])
    assert pad_nets["1"] == "VIN"
    assert pad_nets["2"] == "GND"


def test_pcb_write_produces_loadable_file(tmp_path: Path) -> None:
    p = tmp_path / "board.kicad_pcb"
    pcb.write(_minimal_hdm(), p, footprints_root=_FOOTPRINTS_ROOT)
    assert p.exists()
    # Re-parse to confirm it's well-formed S-expression.
    sexpr.parse(p.read_text())


def test_pcb_emits_edge_cuts_outline_from_dimensions() -> None:
    out = pcb.emit(_minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT)
    # Four Edge.Cuts gr_line segments forming the outline rectangle.
    edge_cut_lines = out.count('(layer "Edge.Cuts")')
    assert edge_cut_lines >= 4
    # Outline is offset by (_ORIGIN_X, _ORIGIN_Y) = (12.5, 12.5), so (start 12.5 12.5) must appear.
    assert "(start 12.5 12.5)" in out
    # Board dims are 50×40 → bottom-right at (62.5, 52.5).
    assert "(end 62.5 52.5)" in out or "(start 62.5 52.5)" in out


def test_pcb_placement_offset_by_working_area_origin() -> None:
    """A component with HDM placement (20, 20) lands at sheet (32.5, 32.5)."""
    out = pcb.emit(_minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT)
    node = sexpr.parse(out)
    footprint = sexpr.find(node, "footprint")
    at_node = sexpr.find(footprint, "at")
    # at = ["at", "32.5", "32.5"] (no rot since 0).
    assert at_node[1] == "32.5" and at_node[2] == "32.5"


def test_pcb_auto_places_when_placement_missing() -> None:
    """Components without explicit placement grid-fill inside the working area."""
    hdm = _minimal_hdm()
    hdm["components"]["J1"].pop("placement", None)
    out = pcb.emit(hdm, footprints_root=_FOOTPRINTS_ROOT)
    node = sexpr.parse(out)
    footprint = sexpr.find(node, "footprint")
    at_node = sexpr.find(footprint, "at")
    x = float(at_node[1])
    y = float(at_node[2])
    # Must be inside working area and NOT inside the title-block notch.
    assert 12.5 <= x <= 284.5 and 12.5 <= y <= 197.5
    assert not (176.5 <= x <= 284.5 and 165.5 <= y <= 197.5)


# ---------------------------------------------------------------------------
# sch
# ---------------------------------------------------------------------------


def test_sch_emit_has_v10_header_and_title() -> None:
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    assert out.startswith("(kicad_sch")
    assert "(version 20260306)" in out
    assert '(title "Emitter Test")' in out


def test_sch_emit_includes_lib_symbols_and_component_instance() -> None:
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    assert "(lib_symbols" in out
    assert '"Connector:Barrel_Jack"' in out
    # Component instance has lib_id reference and refdes property.
    assert '(lib_id "Connector:Barrel_Jack")' in out
    assert '"Reference" "J1"' in out


def test_sch_emits_no_global_label_palette() -> None:
    """The palette of every net name is gone, and must stay gone.

    It was emitted as an editing convenience and cost 193 dangling labels, 117
    collisions with the per-pin labels of the same name, and — because entries
    were spaced tighter than their own text — overlapping labels that shorted
    unrelated nets together in the netlist. Each pin carries its own label.
    """
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    assert "(global_label" not in out
    assert '(label "VIN"' in out and '(label "GND"' in out


def test_sch_write_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "test.kicad_sch"
    sch.write(_minimal_hdm(), p, symbols_root=_SYMBOLS_ROOT)
    sexpr.parse(p.read_text())


def test_sch_emits_wire_plus_label_per_connected_pin() -> None:
    """For each pin appearing in a net, emit a wire stub + label with the net name."""
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    # Barrel_Jack has 2 pins, both are in nets (VIN=1, GND=2) via pin_map.
    node = sexpr.parse(out)
    wires = sexpr.find_all(node, "wire")
    labels = [lab for lab in sexpr.find_all(node, "label")]
    assert len(wires) >= 2
    label_texts = {sexpr.unquote(lab[1]) for lab in labels if len(lab) > 1 and isinstance(lab[1], str)}
    assert "VIN" in label_texts
    assert "GND" in label_texts


def test_a_derived_symbol_reports_the_pins_of_the_parent_it_extends() -> None:
    """KiCad draws most specific part numbers as a shell over a generic body:
    ``2N7002`` carries ``(extends "Q_NMOS_GSD")`` and no pins of its own. Reading
    the shell alone returned an empty list, and nothing downstream treated that
    as an error — the part was placed and drawn with no wires, no labels, and no
    way to say a pin was NC, so every net that reached it silently lost that
    member. On one real board that took the buzzer FET out of BUZZ_GATE and
    BUZZ_DRV, and the review reported them as single-pin nets.

    ``sch.py`` already walks the chain when it copies symbols into
    ``lib_symbols``, which is why the schematic still opened. The two readers
    have to agree, or the file is valid and wrong."""
    shell = loaders.load_symbol_def("Transistor_FET:2N7002", _SYMBOLS_ROOT)
    assert loaders.extends_of(shell) == "Q_NMOS_GSD"   # a shell, by construction

    nums = {p["number"] for p in loaders.load_symbol_pins("Transistor_FET:2N7002", _SYMBOLS_ROOT)}
    assert nums == {p["number"] for p in loaders.load_symbol_pins("Transistor_FET:Q_NMOS_GSD", _SYMBOLS_ROOT)}
    assert len(nums) == 3


def test_a_symbol_that_extends_nothing_is_unaffected() -> None:
    pins = loaders.load_symbol_pins("Device:R", _SYMBOLS_ROOT)
    assert {p["number"] for p in pins} == {"1", "2"}


def test_sch_load_symbol_pins_returns_barrel_jack_pins() -> None:
    """Sanity check the pin-position loader used for wire stubs."""
    pins = loaders.load_symbol_pins("Connector:Barrel_Jack", _SYMBOLS_ROOT)
    nums = {p["number"] for p in pins}
    assert {"1", "2"}.issubset(nums)


# ---------------------------------------------------------------------------
# pro
# ---------------------------------------------------------------------------


def test_pro_emit_has_net_classes_and_patterns() -> None:
    out = pro.emit(_minimal_hdm(), project_filename="test.kicad_pro")
    import json
    data = json.loads(out)
    names = {c["name"] for c in data["net_settings"]["classes"]}
    assert "Default" in names
    patterns = data["net_settings"]["netclass_patterns"]
    assert any(p["pattern"] == "VIN" for p in patterns)
    assert any(p["pattern"] == "GND" for p in patterns)


def test_sch_marks_declared_nc_pins_with_no_connect() -> None:
    """A pin the design says is NC gets KiCad's no-connect X, not a dangling stub."""
    hdm = _minimal_hdm()
    # Pin 2 (GND) leaves the net and is declared NC instead.
    hdm["nets"] = {"VIN": {"class": "Default", "pads": [["J1", "VIN"]]}}
    hdm["components"]["J1"]["no_connect_pins"] = ["2"]
    out = sch.emit(hdm, symbols_root=_SYMBOLS_ROOT)
    node = sexpr.parse(out)
    ncs = sexpr.find_all(node, "no_connect")
    assert len(ncs) == 1
    # Sits exactly on pin 2's connection point, on the 1.27mm grid.
    pins = {p["number"]: p for p in loaders.load_symbol_pins("Connector:Barrel_Jack", _SYMBOLS_ROOT)}
    inst = sexpr.find(node, "symbol")
    cx, cy = float(sexpr.find(inst, "at")[1]), float(sexpr.find(inst, "at")[2])
    tip = sch._pin_absolute_tip(cx, cy, pins["2"])
    at = sexpr.find(ncs[0], "at")
    assert (float(at[1]), float(at[2])) == (sch._snap(tip[0]), sch._snap(tip[1]))
    # And the NC pin got no label — only VIN is labelled.
    labels = [sexpr.unquote(l[1]) for l in sexpr.find_all(node, "label")]
    assert labels == ["VIN"]


def test_sch_without_nc_pins_emits_no_markers() -> None:
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    assert "(no_connect" not in out


def test_sch_load_symbol_pins_gathers_shared_and_unit_one_sub_symbols(tmp_path: Path) -> None:
    """Pins split across _0_0 and _1_0 (the nRF9151 shape) are all unit 1's pins."""
    lib = tmp_path / "Vendor.kicad_sym"
    lib.write_text(
        """(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "SiP" (in_bom yes) (on_board yes)
    (symbol "SiP_0_0"
      (pin power_in line (at 0 10 270) (length 2.54) (name "GND") (number "1"))
    )
    (symbol "SiP_1_0"
      (pin bidirectional line (at 10 0 180) (length 2.54) (name "P0.20") (number "2"))
    )
    (symbol "SiP_1_1"
      (rectangle (start -5 -5) (end 5 5))
    )
    (symbol "SiP_2_1"
      (pin bidirectional line (at -10 0 0) (length 2.54) (name "P0.21") (number "3"))
    )
  )
)""",
        encoding="utf-8",
    )
    numbers = sorted(p["number"] for p in loaders.load_symbol_pins("Vendor:SiP", tmp_path))
    # Unit 2's pin is another unit; the graphics-only _1_1 no longer hides the rest.
    assert numbers == ["1", "2"]


_TWO_UNIT_LIB = """(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "Dual" (in_bom yes) (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (symbol "Dual_1_1"
      (rectangle (start -5.08 -5.08) (end 5.08 5.08))
      (pin input line (at -7.62 0 0) (length 2.54) (name "A") (number "1"))
    )
    (symbol "Dual_2_1"
      (rectangle (start -5.08 -5.08) (end 5.08 5.08))
      (pin power_in line (at 0 7.62 270) (length 2.54) (name "VDD") (number "2"))
    )
  )
)"""


def test_load_symbol_units_groups_pins_by_unit(tmp_path: Path) -> None:
    (tmp_path / "T.kicad_sym").write_text(_TWO_UNIT_LIB, encoding="utf-8")
    units = loaders.load_symbol_units("T:Dual", tmp_path)
    assert {u: [p["number"] for p in pins] for u, pins in units.items()} == {1: ["1"], 2: ["2"]}
    # The unit-1 view is unchanged for callers that only ever wanted one body.
    assert [p["number"] for p in loaders.load_symbol_pins("T:Dual", tmp_path)] == ["1"]


def test_sch_draws_every_unit_of_a_multi_unit_symbol(tmp_path: Path) -> None:
    """One instance per unit, same refdes, each body in its own cell — so the
    power pins an MCU vendor parks on unit 4 are drawn, wired and labelled."""
    (tmp_path / "T.kicad_sym").write_text(_TWO_UNIT_LIB, encoding="utf-8")
    hdm = _minimal_hdm()
    hdm["components"] = {
        "U1": {"value": "Dual", "footprint": "", "lib_symbol": "T:Dual", "pin_map": {"A": "1", "VDD": "2"}}
    }
    hdm["nets"] = {
        "SIG": {"class": "Default", "pads": [["U1", "A"]]},
        "VDD": {"class": "Power_Bulk", "pads": [["U1", "VDD"]]},
    }
    node = sexpr.parse(sch.emit(hdm, symbols_root=[tmp_path, _SYMBOLS_ROOT]))
    insts = [s for s in sexpr.find_all(node, "symbol") if sexpr.unquote(sexpr.find(s, "lib_id")[1]) == "T:Dual"]
    units = sorted(int(sexpr.find(s, "unit")[1]) for s in insts)
    assert units == [1, 2]
    # Both bodies carry the same reference and their instance path names the unit.
    for s in insts:
        ref = next(p for p in sexpr.find_all(s, "property") if sexpr.unquote(p[1]) == "Reference")
        assert sexpr.unquote(ref[2]) == "U1"
        path = sexpr.find(sexpr.find(sexpr.find(s, "instances"), "project"), "path")
        assert sexpr.find(path, "unit")[1] == sexpr.find(s, "unit")[1]
    # They sit in different cells.
    ats = {(sexpr.find(s, "at")[1], sexpr.find(s, "at")[2]) for s in insts}
    assert len(ats) == 2
    # And unit 2's pin got its label — the whole point.
    labels = sorted(sexpr.unquote(l[1]) for l in sexpr.find_all(node, "label"))
    assert labels == ["SIG", "VDD", "VDD"]  # VDD twice: pin stub + PWR_FLAG label
    # The rail audit saw the power_in pin on unit 2 and flagged the rail.
    assert sch.flagged_nets(hdm, symbols_root=[tmp_path, _SYMBOLS_ROOT]) == ["VDD"]


def test_diff_pair_halves_spelled_without_a_separator_still_pair() -> None:
    """DSI_CKP / DSI_CKN is how every MIPI datasheet spells it, and the
    separator-only match missed all of them — so no DSI pair on the example-handheld
    board was ever recognised as a pair, and both halves sat on Default."""
    from blpl.core.stage4_synthesize_nets import _diff_pair_complements as complements

    assert complements("DSI_CKP") == ["DSI_CKN"]
    assert complements("DSI_D0N") == ["DSI_D0P"]
    # The separator pass still wins where both readings exist.
    assert complements("SOM_TX_P") == ["SOM_TX_N"]


def test_usb_d_minus_is_classed_with_its_partner() -> None:
    """The rule matched D[PN]; 'DM' is the ordinary spelling of D-minus, so
    MCU_USB_DP got a class and MCU_USB_DM stayed Default — the two halves of one
    pair under different rules, which cannot be routed as a pair."""
    from blpl.core.stage4_synthesize_nets import _assign_class as klass

    assert klass("MCU_USB_DP") == klass("MCU_USB_DM") == "USB2_Diff_90Ohm"


# --- silkscreen references that cannot fit --------------------------------
#
# A 0402 is 1.0 x 0.5 mm and `C_U1_VDD1` at the 0.8 mm silkscreen minimum is
# about 6 mm wide. The text cannot shrink — 0.8 mm is KiCad's default and the
# board's floor — so it lands on its neighbours and on copper. sb-halow
# reported 21 silk_overlap and 10 silk_over_copper across eleven parts.


def _fp(courtyard, ref_layer="F.SilkS", angle=0, font=0.8, circle=None):
    """A footprint node with a courtyard and a Reference property.

    `circle` gives (radius) for a round courtyard, written the way KiCad does —
    a centre and a point on the circumference, which is the shape that has
    bitten this codebase before.
    """
    from blpl.emitter import sexpr

    node = ["footprint", '"test"']
    if circle is not None:
        node.append(["fp_circle",
                     ["center", "0", "0"],
                     ["end", str(circle), "0"],
                     ["layer", sexpr.quote("F.CrtYd")]])
    elif courtyard is not None:
        w, h = courtyard
        node.append(["fp_rect",
                     ["start", str(-w / 2), str(-h / 2)],
                     ["end", str(w / 2), str(h / 2)],
                     ["layer", sexpr.quote("F.CrtYd")]])
    node.append(["property", '"Reference"', '"REF**"',
                 ["at", "0", "0", str(angle)],
                 ["layer", sexpr.quote(ref_layer)],
                 ["effects", ["font", ["size", str(font), str(font)]]]])
    node.append(["property", '"Value"', '"old"', ["layer", sexpr.quote("F.Fab")]])
    return node


def _ref_hidden(node) -> bool:
    from blpl.emitter import sexpr

    for prop in sexpr.find_all(node, "property"):
        if sexpr.unquote(prop[1]) == "Reference":
            return any(n[1] == "yes" for n in sexpr.find_all(prop, "hide"))
    raise AssertionError("no Reference property")


def test_a_passive_too_small_for_its_reference_loses_the_silk() -> None:
    from blpl.emitter import pcb

    node = _fp((1.0, 0.5))          # 0402
    pcb._set_reference_and_value(node, "C_U1_VDD1", "100nF")
    assert _ref_hidden(node) is True


def test_a_part_big_enough_keeps_its_reference() -> None:
    from blpl.emitter import pcb

    node = _fp((11.5, 10.5))        # a module
    pcb._set_reference_and_value(node, "U_HALOW", "MM8108")
    assert _ref_hidden(node) is False


def test_a_long_narrow_part_is_judged_on_the_axis_the_text_runs_along() -> None:
    """Rotating a footprint turns its courtyard and its reference together, so
    the two keep the same relative orientation and "try it both ways round" is
    not a test of anything. A horizontal reference on a 1.2 mm-wide part has
    1.2 mm to fit in, however long the part is."""
    from blpl.emitter import pcb

    horizontal = _fp((1.2, 16.0), angle=0)
    pcb._set_reference_and_value(horizontal, "J_LINK", "FH69")
    assert _ref_hidden(horizontal) is True

    rotated = _fp((1.2, 16.0), angle=90)
    pcb._set_reference_and_value(rotated, "J_LINK", "FH69")
    assert _ref_hidden(rotated) is False, "a reference running down the part was hidden"


def test_a_circular_courtyard_is_measured_as_a_circle() -> None:
    """`fp_circle` gives a centre and a point on the circumference, not two
    opposite corners. Read as corners the box collapses to a line and every
    round footprint loses its silkscreen — the same mistake placement.py
    records having made about collision boxes."""
    from blpl.emitter import pcb

    node = _fp(None, circle=4.0)          # 8 mm across
    assert pcb._courtyard_extent(node) == (8.0, 8.0)
    pcb._set_reference_and_value(node, "U_BIG", "part")
    assert _ref_hidden(node) is False


def test_the_reference_is_measured_at_the_size_it_will_be_emitted() -> None:
    """Vendor footprints here carry 1.0 mm and 1.27 mm reference fonts.
    Estimating a 0.8 mm label for text that emits at 1.27 mm accepts a
    reference half again as wide as the space measured for it."""
    from blpl.emitter import pcb

    small_font = _fp((4.0, 2.0), font=0.8)
    pcb._set_reference_and_value(small_font, "R_PRES", "0R")
    assert _ref_hidden(small_font) is False

    big_font = _fp((4.0, 2.0), font=1.27)
    pcb._set_reference_and_value(big_font, "R_PRES", "0R")
    assert _ref_hidden(big_font) is True


def test_a_footprint_with_no_courtyard_keeps_its_silk() -> None:
    """Measured, not guessed. With nothing to measure the reference stays —
    hiding it would be a decision made on absent evidence."""
    from blpl.emitter import pcb

    node = _fp(None)
    pcb._set_reference_and_value(node, "C_TINY", "1uF")
    assert _ref_hidden(node) is False


def test_hiding_silk_does_not_touch_the_value_or_the_refdes_itself() -> None:
    """The reference is not lost, only its silk copy: stock footprints carry a
    ${REFERENCE} on F.Fab, which is the assembly drawing a placement operator
    reads from."""
    from blpl.emitter import pcb, sexpr

    node = _fp((1.0, 0.5))
    pcb._set_reference_and_value(node, "R_PRES_M", "0R")
    props = {sexpr.unquote(p[1]): sexpr.unquote(p[2]) for p in sexpr.find_all(node, "property")}
    assert props["Reference"] == "R_PRES_M"
    assert props["Value"] == "0R"


def test_a_reference_already_on_the_fab_layer_is_left_alone() -> None:
    """Only the silkscreen copy is at issue. A footprint whose reference is
    already on F.Fab has nothing to hide and hiding it would remove the refdes
    from the assembly drawing."""
    from blpl.emitter import pcb

    node = _fp((1.0, 0.5), ref_layer="F.Fab")
    pcb._set_reference_and_value(node, "C_TINY", "1uF")
    assert _ref_hidden(node) is False


# --- zero-length courtyard segments ----------------------------------------
#
# A zero-length line has no geometry, so removing one cannot change a shape.
# KiCad disagrees about the consequences, but only on the courtyard: it reports
# each as `malformed_courtyard`, and — far worse — exports **no outline at all**
# for such a footprint into Specctra. The autorouter then has no keep-out for
# the part and routes through the space it occupies. On sb-halow that was the
# largest part on the board, and the "0 unrouted" it reached was achieved by
# routing under a module.


def _fp_with_lines(*segments, layer="F.CrtYd"):
    from blpl.emitter import sexpr

    node = ["footprint", '"t"']
    for seg in segments:
        (x1, y1), (x2, y2) = seg[:2]
        lay = seg[2] if len(seg) > 2 else layer
        node.append(["fp_line",
                     ["start", str(x1), str(y1)],
                     ["end", str(x2), str(y2)],
                     ["layer", sexpr.quote(lay)]])
    return node


def test_a_zero_length_courtyard_segment_is_dropped() -> None:
    from blpl.emitter import pcb, sexpr

    node = _fp_with_lines(((0, 0), (1, 0)), ((1, 0), (1, 0)), ((1, 0), (1, 1)))
    assert pcb._drop_degenerate_lines(node) == 1
    assert len(list(sexpr.find_all(node, "fp_line"))) == 2


def test_the_back_courtyard_counts_too() -> None:
    from blpl.emitter import pcb

    node = _fp_with_lines(((2, 2), (2, 2)), layer="B.CrtYd")
    assert pcb._drop_degenerate_lines(node) == 1


def test_real_segments_are_left_alone() -> None:
    """The check is start == end, not "looks small". A 1 um segment is
    geometry and removing it would change a shape."""
    from blpl.emitter import pcb, sexpr

    node = _fp_with_lines(((0, 0), (0.001, 0)), ((0, 0), (0, 5)))
    assert pcb._drop_degenerate_lines(node) == 0
    assert len(list(sexpr.find_all(node, "fp_line"))) == 2


def test_silkscreen_and_fab_are_not_touched() -> None:
    """Scoped deliberately. 143 footprints across stock kicad-footprints and
    this project carry a zero-length segment, but 137 are on silkscreen or fab
    where it is cosmetic. Stripping those rewrites a footprint to no effect and
    earns a `lib_footprint_mismatch`, because the board copy then differs from
    the library copy. ICM42670P is the worked example: two such segments, both
    F.SilkS, and removing them bought nothing and cost a warning."""
    from blpl.emitter import pcb, sexpr

    node = _fp_with_lines(
        ((-1.9, -0.8), (-1.9, -0.8), "F.SilkS"),
        ((-1.9, -0.7), (-1.9, -0.7), "F.SilkS"),
        ((0, 0), (0, 0), "F.Fab"),
        ((0, 0), (0, 0), "B.SilkS"),
    )
    assert pcb._drop_degenerate_lines(node) == 0
    assert len(list(sexpr.find_all(node, "fp_line"))) == 4


def test_the_strip_runs_on_every_footprint_not_just_legacy_ones() -> None:
    """The defect is not confined to pre-v10 files: stock kicad-footprints
    carries it in SOT-723, Telit_xL865 and nRF24L01_Breakout, all current. A
    v10 footprint has to be cleaned too."""
    from blpl.emitter import pcb, sexpr

    node = _fp_with_lines(((2, 2), (2, 2)))
    node.insert(1, ["version", "20240108"])      # unmistakably modern
    pcb._upgrade_legacy_footprint(node)
    assert list(sexpr.find_all(node, "fp_line")) == []


def test_a_malformed_line_without_coordinates_is_not_dropped() -> None:
    """Refusing to parse is not evidence of being degenerate."""
    from blpl.emitter import pcb, sexpr

    node = ["footprint", '"t"',
            ["fp_line", ["start", "0"], ["end", "0"], ["layer", '"F.CrtYd"']]]
    assert pcb._drop_degenerate_lines(node) == 0
    assert len(list(sexpr.find_all(node, "fp_line"))) == 1


def test_a_line_with_no_layer_is_not_dropped() -> None:
    """Unknown layer is not the courtyard."""
    from blpl.emitter import pcb, sexpr

    node = ["footprint", '"t"',
            ["fp_line", ["start", "0", "0"], ["end", "0", "0"]]]
    assert pcb._drop_degenerate_lines(node) == 0
    assert len(list(sexpr.find_all(node, "fp_line"))) == 1
