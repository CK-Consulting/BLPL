"""Stage 6a with KiCad Routing Tools (blpl.core.krt_route)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blpl.core import autoroute, krt_route

PRO = {"net_settings": {
    "classes": [
        {"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Power_Bulk", "track_width": 0.75, "clearance": 0.25, "via_diameter": 0.9, "via_drill": 0.5},
        {"name": "RF_50R", "track_width": 0.35, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
    ],
    "netclass_patterns": [
        {"pattern": "GND", "netclass": "Power_Bulk"}, {"pattern": "VBAT", "netclass": "Power_Bulk"},
        {"pattern": "RF_*", "netclass": "RF_50R"},
    ],
}}
BOARD = '(kicad_pcb (net 0 "") (net 1 "GND") (net 2 "VBAT") (net 3 "RF_ANT") (net 4 "SPI_SCK"))'


@pytest.fixture
def board(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD)
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(PRO))
    return pcb


def test_nets_are_grouped_by_their_class(board):
    groups, _ = krt_route.nets_by_class(board, board.with_suffix(".kicad_pro"))
    assert groups == {"Power_Bulk": ["GND", "VBAT"], "RF_50R": ["RF_ANT"], "Default": ["SPI_SCK"]}


def test_planes_then_each_class_at_its_width_then_the_rest_with_power_nets(board):
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert [s["script"] for s in steps] == ["route_planes.py", "route.py", "route.py"]
    planes, rf, rest = (s["args"] for s in steps)
    # The plane vias at the plane net's own class: 0.25 mm, a 0.9/0.5 via.
    assert planes[:4] == ["--nets", "GND", "--plane-layers", "In1.Cu"] and "0.25" in planes and "0.9" in planes
    # The RF class at 0.35 mm, never escalated below the project's floors.
    assert rf[:2] == ["--nets", "RF_ANT"] and rf[rf.index("--track-width") + 1] == "0.35"
    assert "--escalation" in rf and rf[rf.index("--escalation") + 1] == "off" and "--no-fix-drc-settings" in rf
    # Everything else at Default, the power class as power nets at 0.75 mm.
    assert rest[:2] == ["--nets", "*"] and rest[rest.index("--track-width") + 1] == "0.2"
    i = rest.index("--power-nets")
    assert rest[i + 1:i + 3] == ["GND", "VBAT"] and rest[rest.index("--power-nets-widths") + 1:][:2] == ["0.75", "0.75"]


def test_the_project_file_survives_every_step(board, tmp_path, monkeypatch):
    pro = board.with_suffix(".kicad_pro")
    original = pro.read_text()
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {"GND": ["In1.Cu"]})
    seen = []

    def clobber(*a, **k):
        # pcbnew's save and the router both rewrite the project file.
        pro.write_text('{"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}')
        return subprocess.CompletedProcess([], 0, "", "")

    def run(cmd, timeout):
        seen.append((Path(cmd[1]).name, pro.read_text() == original))
        return clobber()

    monkeypatch.setattr(krt_route, "_resave", clobber)
    monkeypatch.setattr(krt_route, "_fill", clobber)
    monkeypatch.setattr(krt_route, "_run", run)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 3, "segments": 400})
    r = krt_route.route(board, work_dir=tmp_path / "w")
    assert r.ok and r.unrouted == 3 and r.extra["router"] == "kicad-routing-tools"
    assert seen and all(intact for _, intact in seen), seen
    assert pro.read_text() == original


def test_a_failing_step_stops_the_route_and_says_which(board, tmp_path, monkeypatch):
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: subprocess.CompletedProcess(cmd, 2, "", "boom"))
    r = krt_route.route(board, work_dir=tmp_path / "w")
    assert not r.ok and r.reason.startswith("route.py exited 2")


def test_run_for_names_an_unknown_router_and_skips(board, tmp_path):
    r = autoroute.run_for(board, tmp_path / "r.json", router="nope")
    assert not r.attempted and "unknown router 'nope'" in r.reason


def test_a_missing_install_is_a_skip_with_a_reason(board, tmp_path, monkeypatch):
    monkeypatch.setenv("KRT_HOME", str(tmp_path / "absent"))
    monkeypatch.setattr(krt_route, "DEFAULT_HOME", tmp_path / "absent")
    r = autoroute.run_for(board, tmp_path / "r.json", router="kicad-routing-tools")
    assert not r.attempted and "KiCad Routing Tools" in r.reason


def test_the_schema_takes_the_router():
    from blpl.core import schema

    base = {"project": {"name": "p", "dimensions": [10, 10]}}
    schema.validate("project_config", {**base, "routing": {"router": "kicad-routing-tools"}})
    with pytest.raises(Exception):
        schema.validate("project_config", {**base, "routing": {"router": "topor"}})


def test_the_review_names_the_router_that_ran():
    from blpl.core import stage8_review

    def routing(**kw):
        md = stage8_review._render_markdown({
            "summary": {"emitter": 0, "design": 0, "expected": 0}, "placeholders": [], "emitter_defects": [], "design_issues": [], "expected": [],
            "expected_issues": [], "autoroute": {"attempted": True, "ok": True, "unrouted": 2, **kw}})
        return md[md.index("## Routing"):].splitlines()[2]

    assert routing(router="kicad-routing-tools") == "KiCad Routing Tools ran; 2 net(s) left unrouted."
    assert routing(passes=10) == "Freerouting ran (10 passes); 2 net(s) left unrouted."


@pytest.mark.parametrize("yaml_text, installed, expected", [
    ("routing: {router: freerouting}\n", True, "freerouting"),        # named outright wins
    ("routing: {router: kicad-routing-tools}\n", False, "kicad-routing-tools"),
    ("", True, "kicad-routing-tools"),                                 # the default when installed
    ("", False, "freerouting"),                                        # the backup when it is not
])
def test_the_router_stage6a_uses(tmp_path, monkeypatch, yaml_text, installed, expected):
    import argparse

    from blpl.core import cli

    (tmp_path / "project.yaml").write_text("project: {name: p, dimensions: [10, 10]}\n" + yaml_text)
    monkeypatch.setattr(krt_route, "available", lambda: (installed, "" if installed else "missing"))
    assert cli._router(argparse.Namespace(project_dir=str(tmp_path), board=None), tmp_path) == expected


def test_the_adapted_skills_ship_and_credit_their_source():
    from blpl.core import skills_install

    avail = skills_install.available()
    for name in ("routed-board-review", "plane-mappings"):
        assert name in avail and name in skills_install.DEFAULT_SET
        text = (avail[name] / "SKILL.md").read_text()
        assert "github.com/drandyhaas/KiCadRoutingTools" in text and "ACKNOWLEDGMENTS.md" in text


def test_plane_vias_stay_off_pads_and_signals_off_the_planes(board):
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"], "VBAT": ["In2.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    planes, *routes = (s["args"] for s in steps)
    assert planes[planes.index("--same-net-pad-clearance") + 1] == "0.25"
    for args in routes:
        i = args.index("--layers")
        assert args[i + 1:i + 3] == ["F.Cu", "B.Cu"]


def test_the_copper_layers_are_read_from_the_board(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user)))')
    assert krt_route.board_copper(pcb) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_the_boards_edge_and_hole_rules_reach_every_route(board):
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5, "min_hole_to_hole": 0.25}}}
    pro.write_text(json.dumps(data))
    for step in krt_route.plan(board, pro, {}, ["F.Cu", "B.Cu"]):
        a = step["args"]
        assert a[a.index("--board-edge-clearance") + 1] == "0.5"
        assert a[a.index("--hole-to-hole-clearance") + 1] == "0.25"
