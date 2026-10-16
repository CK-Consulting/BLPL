"""The schematic is drawn as a schematic, not as a grid of parts.

The emitter used to put every part in a cell of one size, in board order, with
a stub and a local label on every pin: electrically complete and useless to
anyone reviewing the design from it. These pin down what replaced it — parts
grouped with the IC they serve, turned the way an engineer draws them, nets
named the way the board names them — and that nothing on the sheet overlaps.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from blpl.emitter import sch, sch_layout, sexpr
from blpl.emitter.sch_layout import net_kind, pin_tip

_SYMBOLS_ROOT = Path(__file__).resolve().parent.parent / "kicad-symbols"


def _part(ref, lib, nets, x, y, value="", near=None):
    comp = {"value": value or ref, "footprint": "", "lib_symbol": lib,
            "placement": {"x": x, "y": y, "rot": 0.0, "side": "top"}}
    if near:
        comp["near"] = near
    return ref, comp, nets


def _hdm(*parts):
    comps, nets = {}, {}
    for ref, comp, pin_nets in parts:
        comps[ref] = comp
        for pin, net in pin_nets.items():
            nets.setdefault(net, {"class": "Default", "pads": []})["pads"].append([ref, pin])
    return {"project": {"name": "T", "dimensions": [50, 50]}, "components": comps, "nets": nets}


# Two ICs sharing a rail and a bus, with the passives a real board has.
U_A = _part("U1", "Connector_Generic:Conn_01x06", {"1": "VDD_3V3", "2": "GND", "3": "SDA", "4": "SCL", "5": "GND", "6": "GND"}, 10, 10)
U_B = _part("U2", "Connector_Generic:Conn_01x06", {"1": "VDD_3V3", "2": "GND", "3": "SDA", "4": "SCL", "5": "VDD_RF", "6": "IRQ"}, 40, 40)


def _layout(hdm):
    components = hdm["components"]
    units = sch._load_units(components, _SYMBOLS_ROOT)
    pin_nets = sch._build_component_pin_net_map(hdm)
    parts = {r: [(u, units[c["lib_symbol"]][u]) for u in sorted(units[c["lib_symbol"]])] for r, c in components.items()}
    positions = {r: (c["placement"]["x"], c["placement"]["y"]) for r, c in components.items()}
    near = {r: c["near"] for r, c in components.items() if c.get("near")}
    blocks = sch_layout.assign_blocks(parts, pin_nets, positions, {r: r for r in parts}, near)
    texts = {r: (r, c["value"]) for r, c in components.items()}
    return blocks, sch_layout.layout(blocks, parts, pin_nets, texts), pin_nets, texts


def _block_of(blocks, ref):
    return next(b.anchor for b in blocks if any(r == ref for r, _ in b.members))


def test_nets_are_sorted_into_what_ends_them():
    assert [net_kind(n) for n in ("GND", "AGND", "VSS")] == ["ground"] * 3
    assert [net_kind(n) for n in ("VDD_3V3", "VBAT_PROT", "+3V3", "VDD_LR2021", "VBUS")] == ["rail"] * 5
    # Starting with V does not make a net a rail.
    assert [net_kind(n) for n in ("SPI_SCK", "I2C_SDA", "VSYNC_DET")] == ["signal"] * 3


def test_a_part_joins_the_ic_its_connections_row_names():
    # C1 sits on VDD_3V3 and GND, which both ICs share: only the design's
    # "at U2" says whose it is. Nearest on the board would have said U1.
    cap = _part("C1", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 11, 11, "100nF", near="U2")
    blocks, *_ = _layout(_hdm(U_A, U_B, cap))
    assert _block_of(blocks, "C1") == "U2"


def test_a_rail_only_one_ic_uses_outranks_one_they_all_use():
    # A ferrite feeding U2's own rail belongs to U2, wherever the board put it.
    bead = _part("FB1", "Device:FerriteBead", {"1": "VDD_3V3", "2": "VDD_RF"}, 11, 11)
    blocks, *_ = _layout(_hdm(U_A, U_B, bead))
    assert _block_of(blocks, "FB1") == "U2"


def test_a_signal_outranks_a_rail_and_ground_decides_nothing():
    pullup = _part("R1", "Device:R", {"1": "IRQ", "2": "VDD_3V3"}, 11, 11)
    blocks, *_ = _layout(_hdm(U_A, U_B, pullup))
    assert _block_of(blocks, "R1") == "U2"


def test_with_nothing_else_to_go_by_the_nearest_ic_on_the_board_wins():
    cap = _part("C2", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 41, 41)
    blocks, *_ = _layout(_hdm(U_A, U_B, cap))
    assert _block_of(blocks, "C2") == "U2"


@pytest.mark.parametrize("lib,nets,want", [
    ("Device:C", {"1": "GND", "2": "VDD_3V3"}, {"2": 90, "1": 270}),       # decoupling: rail up
    ("Device:R", {"1": "SDA", "2": "VDD_3V3"}, {"2": 90, "1": 270}),       # pull-up: rail up, signal down
    ("Device:R", {"1": "SPI_SCK", "2": "SPI_SCK_R"}, {"1": 180, "2": 0}),  # series: on its side, in to out
    ("Device:D", {"1": "VBAT_PROT", "2": "VBAT_RAW"}, {"1": 180, "2": 0}),  # two rails: on its side
])
def test_small_parts_are_turned_the_way_an_engineer_draws_them(lib, nets, want):
    from blpl.emitter import loaders

    pins = loaders.load_symbol_units(lib, _SYMBOLS_ROOT)[1]
    rot = sch_layout._two_pin_rotation(pins, nets)
    assert {p["number"]: pin_tip(p, 0, 0, rot)[2] for p in pins} == want


def test_a_turned_pin_still_lands_on_the_grid():
    pin = {"x": 0.0, "y": 3.81, "rot": 270.0}  # Device:R pin 1: top, pointing in
    assert pin_tip(pin, 100.33, 100.33, 0) == (100.33, 96.52, 90)
    assert pin_tip(pin, 100.33, 100.33, 90)[2] == 180  # turned a quarter: now points left


def test_nothing_on_the_sheet_overlaps():
    # Overlap is not cosmetic: KiCad reads touching pins and labels as wired.
    parts = [U_A, U_B,
             _part("C1", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 11, 11, "100nF"),
             _part("C2", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 11, 11, "4.7µF"),
             _part("R1", "Device:R", {"1": "SDA", "2": "VDD_3V3"}, 11, 11, "4.7kΩ"),
             _part("R2", "Device:R", {"1": "SCL", "2": "VDD_3V3"}, 11, 11, "4.7kΩ"),
             _part("FB1", "Device:FerriteBead", {"1": "VDD_3V3", "2": "VDD_RF"}, 41, 41, "1kΩ")]
    blocks, placed, pin_nets, texts = _layout(_hdm(*parts))
    frames = [(b.x0, b.y0, b.x1, b.y1) for b in blocks]
    boxes = []
    for p in placed:
        x0, y0, x1, y1 = sch_layout.part_extent(p.pins, p.rot, pin_nets.get(p.ref, {}),
                                                texts.get(p.ref) if len(p.pins) <= 2 else None)
        boxes.append((p.ref, (p.x + x0, p.y + y0, p.x + x1, p.y + y1)))

    def overlap(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    assert not [(a, b) for i, a in enumerate(frames) for b in frames[i + 1:] if overlap(a, b)]
    assert not [(ra, rb) for i, (ra, a) in enumerate(boxes) for rb, b in boxes[i + 1:] if overlap(a, b)]
    for ref, box in boxes:
        frame = next(f for f, b in zip(frames, blocks) if any(r == ref for r, _ in b.members))
        assert frame[0] <= box[0] and frame[1] <= box[1] and box[2] <= frame[2] and box[3] <= frame[3], ref


def test_nets_are_named_as_the_board_names_them():
    # Local labels name their net "/SDA" and the board's pads "SDA": that
    # alone was 87 schematic-parity errors on one board.
    parts = [U_A, _part("C1", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 11, 11, "100nF")]
    node = sexpr.parse(sch.emit(_hdm(*parts), symbols_root=_SYMBOLS_ROOT))
    assert not sexpr.find_all(node, "label")
    assert "SDA" in {sexpr.unquote(g[1]) for g in sexpr.find_all(node, "global_label")}
    # The capacitor's rail is a power symbol whose Value is the net; U1's
    # VDD_3V3 pin, on an IC, is a label of the same name.
    powers = [s for s in sexpr.find_all(node, "symbol") if sexpr.unquote(sexpr.find(s, "lib_id")[1]) == "power:VCC"]
    values = {sexpr.unquote(next(pr for pr in sexpr.find_all(s, "property") if sexpr.unquote(pr[1]) == "Value")[2]) for s in powers}
    assert values == {"VDD_3V3"}


def test_adjacent_pins_on_one_net_share_one_terminal_and_never_jump_a_pin():
    # Conn pins 5 and 6 are GND side by side; pin 2 is GND too but with 3 and
    # 4 between. Tied, 5–6 take one label; 2 takes its own. A tie that jumped
    # pins once shorted RF_LRHF to GND.
    node = sexpr.parse(sch.emit(_hdm(U_A), symbols_root=_SYMBOLS_ROOT))
    gnd = [g for g in sexpr.find_all(node, "global_label") if sexpr.unquote(g[1]) == "GND"]
    assert len(gnd) == 2
    assert len(sexpr.find_all(node, "junction")) == 0  # a run of two has no middle


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_reads_every_pin_onto_the_net_the_design_gives_it(tmp_path):
    parts = [U_A, U_B,
             _part("C1", "Device:C", {"1": "VDD_3V3", "2": "GND"}, 11, 11, "100nF", near="U1"),
             _part("R1", "Device:R", {"1": "SDA", "2": "VDD_3V3"}, 11, 11, "4.7kΩ"),
             _part("R2", "Device:R", {"1": "IRQ", "2": "IRQ_R"}, 41, 41, "470Ω"),
             _part("FB1", "Device:FerriteBead", {"1": "VDD_3V3", "2": "VDD_RF"}, 41, 41, "1kΩ")]
    hdm = _hdm(*parts)
    sch.write(hdm, tmp_path / "t.kicad_sch", symbols_root=_SYMBOLS_ROOT)
    (tmp_path / "t.kicad_pro").write_text("{}")
    subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
                    "-o", str(tmp_path / "n.xml"), str(tmp_path / "t.kicad_sch")],
                   check=True, capture_output=True)
    import xml.etree.ElementTree as ET

    got = {(n.get("ref"), n.get("pin")): net.get("name")
           for net in ET.parse(tmp_path / "n.xml").getroot().iter("net") for n in net.iter("node")}
    want = {(r, str(p)): name for name, net in hdm["nets"].items() for r, p in net["pads"]}
    assert {k: got.get(k) for k in want} == want
