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


def test_load_symbol_shell_strips_graphics() -> None:
    shell = loaders.load_symbol_shell("Connector:Barrel_Jack", _SYMBOLS_ROOT)
    # The shell's name atom is the "Lib:Name" form.
    assert sexpr.unquote(shell[1]) == "Connector:Barrel_Jack"
    # And the nested unit symbol (the sub-symbol carrying pins/graphics) is dropped.
    nested_units = [c for c in shell if isinstance(c, list) and sexpr.head(c) == "symbol"]
    assert nested_units == []
    # Properties are retained, but their *value* atom (the 3rd token) is blanked.
    props = sexpr.find_all(shell, "property")
    assert len(props) > 0
    for p in props:
        assert p[2] == '""'


# ---------------------------------------------------------------------------
# pcb
# ---------------------------------------------------------------------------


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


def test_sch_emit_has_global_labels_for_each_net() -> None:
    out = sch.emit(_minimal_hdm(), symbols_root=_SYMBOLS_ROOT)
    assert '(global_label "VIN"' in out
    assert '(global_label "GND"' in out


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
