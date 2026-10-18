"""Ground and supply planes on the inner layers, and getting pads onto them."""

from __future__ import annotations

import pytest

from blpl.core import autoroute, plane_fanout, planes
from blpl.emitter import pcb, sexpr

OUTLINE = [[0, 0], [40, 0], [40, 30], [0, 30]]


def _nets(**counts):
    return {n: {"pads": [["U1", str(i)] for i in range(c)]} for n, c in counts.items()}


def _cfg(layers, zones=None):
    cfg = {"project": {"name": "t", "dimensions": [40, 30], "stackup": {"layers": layers}}}
    if zones is not None:
        cfg["boundaries"] = {"copper_zones": zones}
    return cfg


def test_a_four_layer_board_gets_ground_and_its_busiest_rail_by_default():
    nets = _nets(GND=20, VDD_3V3=9, VBAT_PROT=12, VBAT_RAW=30, SDA=2)
    got = planes.resolve(_cfg(4, []), nets, OUTLINE)
    # VBAT_RAW has the most pads but is the unprotected cell side: never a plane.
    assert [(p["net"], p["layer"]) for p in got] == [("GND", "In1.Cu"), ("VBAT_PROT", "In2.Cu")]
    assert all(p["polygon"] == OUTLINE and p["source"] == "default" for p in got)


def test_two_layers_or_none_means_no_planes():
    nets = _nets(GND=4, VDD_3V3=4)
    assert planes.resolve(_cfg(2), nets, OUTLINE) == []
    assert planes.resolve(_cfg(4, "none"), nets, OUTLINE) == []


def test_declared_planes_are_exactly_those_and_checked():
    nets = _nets(GND=4, VDD_3V3=4)
    got = planes.resolve(_cfg(4, [{"net": "VDD_3V3", "layer": "In1.Cu"}]), nets, OUTLINE)
    assert [(p["net"], p["layer"], p["source"]) for p in got] == [("VDD_3V3", "In1.Cu", "project.yaml")]
    with pytest.raises(planes.PlaneConfigError, match="not on this board"):
        planes.resolve(_cfg(4, [{"net": "AVDD", "layer": "In1.Cu"}]), nets, OUTLINE)
    with pytest.raises(planes.PlaneConfigError, match="In3.Cu"):
        planes.resolve(_cfg(4, [{"net": "GND", "layer": "In3.Cu"}]), nets, OUTLINE)


def test_a_plane_is_a_zone_on_its_net_and_its_layer_is_a_power_layer(tmp_path):
    hdm = {
        "project": {"name": "t", "dimensions": [40, 30], "stackup": {"layers": 4}},
        "components": {},
        "nets": {"GND": {"class": "Default", "pads": []}},
        "board_geometry": {
            "outline": {"segments": [{"kind": "line", "points": [OUTLINE[i], OUTLINE[(i + 1) % 4]]}
                                     for i in range(4)], "polygon": OUTLINE},
            "features": [],
            "planes": [{"net": "GND", "layer": "In1.Cu", "polygon": OUTLINE, "source": "default"}],
        },
    }
    node = pcb.build(hdm, footprints_root=tmp_path)
    zones = [z for z in sexpr.find_all(node, "zone") if sexpr.find(z, "keepout") is None]
    assert len(zones) == 1 and sexpr.unquote(sexpr.find(zones[0], "net_name")[1]) == "GND"
    layers = sexpr.find(node, "layers")
    kinds = {sexpr.unquote(str(row[1])): row[2] for row in layers[1:] if len(row) > 2}
    # Marked power, Freerouting is told it is a plane and keeps signals off it.
    assert kinds["In1.Cu"] == "power" and kinds["In2.Cu"] == "signal"
    out = tmp_path / "b.kicad_pcb"
    out.write_text(sexpr.dump_top(node))
    assert autoroute.plane_nets(out) == ["GND"]


def test_the_fanout_answer_is_read_back_and_a_silent_script_is_an_error():
    out = 'noise\nFANOUT {"placed": [["C1", "2", "GND"]], "failed": [["U1", "4", "GND"]]}\n'
    assert plane_fanout.parse(out) == {"placed": [["C1", "2", "GND"]], "failed": [["U1", "4", "GND"]]}
    assert "error" in plane_fanout.parse("")
    assert "GND" in plane_fanout.script("/x.kicad_pcb", ["GND"])
