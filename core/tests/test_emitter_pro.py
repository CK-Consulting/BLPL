"""The .kicad_pro emitter writes net classes KiCad 10 actually applies."""

from __future__ import annotations

import json
import subprocess
import textwrap

import pytest

from blpl.core import autoroute
from blpl.emitter import pro

HDM = {
    "net_classes": {
        "Default": {"trace_width": 0.2, "clearance": 0.15},
        "Power_Bulk": {"trace_width": 0.75, "clearance": 0.25, "via_dia": 0.9, "via_drill": 0.5},
    },
    "nets": {"VBUS": {"class": "Power_Bulk"}, "SPI_SCK": {"class": "Default"}},
}


def test_classes_carry_kicad10s_version_and_priorities():
    """The regression test. KiCad 10.0.0, the version in the pipeline's
    images, applies a class only with these; 10.0.7 copes without them, so the
    check below passes on a newer host either way and cannot stand in for it."""
    ns = pro.build(HDM, project_filename="b.kicad_pro")["net_settings"]
    assert ns["meta"] == {"version": 4}
    by = {c["name"]: c for c in ns["classes"]}
    assert by["Default"]["priority"] == 2147483647
    assert by["Power_Bulk"]["priority"] == 0
    assert by["Power_Bulk"]["clearance"] == 0.25 and by["Power_Bulk"]["track_width"] == 0.75


@pytest.mark.skipif(autoroute.find_pcbnew_python() is None, reason="needs a Python that imports pcbnew")
def test_kicad_applies_the_emitted_classes(tmp_path):
    """KiCad itself, given the emitted project, applies each class's clearance.
    Under KiCad 10.0.0 the old file had every net at the built-in 0.2 mm."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(textwrap.dedent('''\
        (kicad_pcb (version 20240108) (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
          (net 0 "") (net 1 "VBUS") (net 2 "SPI_SCK")
          (segment (start 0 0) (end 5 0) (width 0.75) (layer "F.Cu") (net 1))
          (segment (start 0 2) (end 5 2) (width 0.2) (layer "F.Cu") (net 2))
        )
    '''))
    pcb.with_suffix(".kicad_pro").write_text(pro.emit(HDM, project_filename="b.kicad_pro"))
    code = textwrap.dedent(f'''\
        import json, pcbnew
        b = pcbnew.LoadBoard({str(pcb)!r})
        out = {{}}
        for t in b.GetTracks():
            out[t.GetNetname()] = round(pcbnew.ToMM(t.GetOwnClearance(t.GetLayer())), 3)
        print("CLR " + json.dumps(out))
    ''')
    proc = subprocess.run([autoroute.find_pcbnew_python(), "-c", code], capture_output=True, text=True, timeout=120)
    line = next(l for l in proc.stdout.splitlines() if l.startswith("CLR "))
    assert json.loads(line[4:]) == {"VBUS": 0.25, "SPI_SCK": 0.15}


def test_the_board_rules_drc_applies_are_written_out_for_the_router():
    """Left implicit, KiCad's DRC used its defaults and the router, reading the
    file, used none."""
    from blpl.emitter import pro

    rules = pro.build({"nets": {}}, project_filename="x.kicad_pro")["board"]["design_settings"]["rules"]
    assert rules["min_copper_edge_clearance"] == 0.5
    assert rules["min_hole_clearance"] == 0.25
    assert rules["min_hole_to_hole"] == 0.25


def test_clipping_silk_turns_off_the_library_mismatch_check_and_only_that():
    """Every footprint clip_to_copper trims differs from its library copy by
    design: 28 lib_footprint_mismatch warnings on verdacor said only that."""
    from blpl.emitter import pro

    clipped = pro.build({"silkscreen": {"clip_to_copper": True}}, project_filename="x.kicad_pro")
    assert clipped["board"]["design_settings"]["rule_severities"] == {"lib_footprint_mismatch": "ignore"}
    plain = pro.build({}, project_filename="x.kicad_pro")
    assert "rule_severities" not in plain["board"]["design_settings"]
