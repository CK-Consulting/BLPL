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
    pro = board.with_suffix(".kicad_pro")
    shared = json.loads(pro.read_text())
    # Power_Bulk on Default's vias, so it shares the last pass.
    shared["net_settings"]["classes"][1].update(via_diameter=0.6, via_drill=0.3)
    pro.write_text(json.dumps(shared))
    steps = krt_route.plan(board, pro, {"GND": ["In1.Cu"]}, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert [s["script"] for s in steps] == ["route_planes.py", "route.py", "route.py"]
    planes, rf, rest = (s["args"] for s in steps)
    # The plane vias at the plane net's own class: 0.25 mm.
    assert planes[:4] == ["--nets", "GND", "--plane-layers", "In1.Cu"] and "0.25" in planes
    # The RF class at 0.35 mm, never escalated below the project's floors.
    assert rf[:2] == ["--nets", "RF_ANT"] and rf[rf.index("--track-width") + 1] == "0.35"
    assert "--escalation" in rf and rf[rf.index("--escalation") + 1] == "off" and "--no-fix-drc-settings" in rf
    # Everything else at Default, the power class as power nets at 0.75 mm,
    # named outright: never the RF net again at the Default width.
    assert rest[:4] == ["--nets", "GND", "SPI_SCK", "VBAT"] and rest[rest.index("--track-width") + 1] == "0.2"
    assert "RF_ANT" not in rest and "*" not in rest
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
        assert args[args.index("--same-net-pad-clearance") + 1] == "0.25"


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


def test_a_net_planed_on_two_layers_is_poured_on_both(board):
    (planes, *_) = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu", "In2.Cu"]},
                                  ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    a = planes["args"]
    assert a[:6] == ["--nets", "GND", "GND", "--plane-layers", "In1.Cu", "In2.Cu"]


def test_each_plane_class_gets_its_own_vias(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text('(kicad_pcb (net 1 "GND") (net 2 "VDD") (net 3 "SIG"))')
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({"net_settings": {"classes": [
        {"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Gnd", "track_width": 0.5, "clearance": 0.2, "via_diameter": 0.9, "via_drill": 0.5},
        {"name": "Vdd", "track_width": 0.5, "clearance": 0.3, "via_diameter": 1.1, "via_drill": 0.6}],
        "netclass_patterns": [{"pattern": "GND", "netclass": "Gnd"}, {"pattern": "VDD", "netclass": "Vdd"}]}}))
    steps = krt_route.plan(pcb, pro, {"GND": ["In1.Cu"], "VDD": ["In2.Cu"]}, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    planes = [s["args"] for s in steps if s["script"] == "route_planes.py"]
    assert len(planes) == 2
    gnd, vdd = sorted(planes, key=lambda a: a[1])
    assert gnd[:4] == ["--nets", "GND", "--plane-layers", "In1.Cu"] and gnd[gnd.index("--via-size") + 1] == "0.9"
    assert vdd[:4] == ["--nets", "VDD", "--plane-layers", "In2.Cu"] and vdd[vdd.index("--via-size") + 1] == "1.1"


def test_an_edge_band_goes_on_before_routing_and_comes_off_after(board, tmp_path, monkeypatch):
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5}}}
    pro.write_text(json.dumps(data))
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    calls = []
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: calls.append("resave") or ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add: calls.append(("band", add, d)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: calls.append(Path(cmd[1]).name) or ok)
    monkeypatch.setattr(krt_route, "_fill", lambda *a: calls.append("fill") or ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    assert krt_route.route(board, work_dir=tmp_path / "w").ok
    # Band on after the re-save, before any routing; off before the fill.
    # Its depth is the edge rule plus half the widest track (0.75 / 2).
    assert calls[:2] == ["resave", ("band", True, 0.875)]
    assert calls[-2:] == [("band", False, 0), "fill"]
    assert all("route" in c for c in calls[2:-2])


def _two_class_board(tmp_path, nets, classes, patterns):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb " + " ".join(f'(net {i} "{n}")' for i, n in enumerate(nets, 1)) + ")")
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({"net_settings": {"classes": classes, "netclass_patterns": patterns}}))
    return pcb, pro


def test_a_differential_class_goes_to_the_differential_router(tmp_path):
    pcb, pro = _two_class_board(tmp_path, ["USB_P", "USB_N", "SIG"], [
        {"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "USB_90R", "track_width": 0.2, "clearance": 0.15, "diff_pair_width": 0.18, "diff_pair_gap": 0.15,
         "via_diameter": 0.6, "via_drill": 0.3}],
        [{"pattern": "USB_*", "netclass": "USB_90R"}])
    steps = krt_route.plan(pcb, pro, {}, ["F.Cu", "B.Cu"])
    (diff,) = [s for s in steps if s["script"] == "route_diff.py"]
    a = diff["args"]
    assert a[a.index("--track-width") + 1] == "0.18" and a[a.index("--diff-pair-gap") + 1] == "0.15"
    assert krt_route._diff_pairs(["A_P", "A_N", "B"]) == []


def test_a_pair_whose_gap_is_under_its_clearance_is_refused_not_widened(tmp_path, monkeypatch):
    # route_diff.py raises such a gap to the clearance: the pair would be laid
    # 0.2 mm apart under a step claiming 0.15, at another impedance.
    pcb, pro = _two_class_board(tmp_path, ["USB_DP", "USB_DM", "SIG"], [
        {"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "USB_90R", "track_width": 0.2, "clearance": 0.2, "diff_pair_width": 0.18, "diff_pair_gap": 0.15,
         "via_diameter": 0.6, "via_drill": 0.3}],
        [{"pattern": "USB_*", "netclass": "USB_90R"}])
    refused = []
    steps = krt_route.plan(pcb, pro, {}, ["F.Cu", "B.Cu"], refused=refused)
    assert [s["script"] for s in steps] == ["route.py"] and "USB_DP" not in steps[0]["args"]
    assert refused == [{"class": "USB_90R", "nets": ["USB_DM", "USB_DP"], "reason": refused[0]["reason"]}]
    assert "0.15" in refused[0]["reason"] and "0.2" in refused[0]["reason"]
    # route() carries the refusal into its report.
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    for fn in ("_resave", "_edge_band", "_fill"):
        monkeypatch.setattr(krt_route, fn, lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 2, "segments": 10})
    r = krt_route.route(pcb, work_dir=tmp_path / "w")
    assert r.to_dict()["refused"][0]["class"] == "USB_90R"


def test_later_passes_never_take_up_an_earlier_class(board):
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, ["F.Cu", "B.Cu"])
    rest = steps[-1]["args"]
    assert "![R]F_ANT" in rest and "RF_ANT" not in rest


def test_vias_stay_off_pads_with_no_planes(board):
    for step in krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, ["F.Cu", "B.Cu"]):
        a = step["args"]
        assert a[a.index("--same-net-pad-clearance") + 1] == "0.15"


def test_a_power_class_with_its_own_vias_routes_in_its_own_pass(board):
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, ["F.Cu", "B.Cu"])
    assert [s["script"] for s in steps] == ["route.py", "route.py", "route.py"]
    _, power, rest = (s["args"] for s in steps)
    # Power_Bulk's 0.9/0.5 vias, never the Default 0.6/0.3 the shared pass
    # would apply to every net it selects.
    assert power[:3] == ["--nets", "GND", "VBAT"] and "![R]F_ANT" in power
    assert power[power.index("--via-size") + 1] == "0.9" and power[power.index("--via-drill") + 1] == "0.5"
    i = power.index("--power-nets")
    assert power[i + 1:i + 3] == ["GND", "VBAT"]
    assert rest[:2] == ["--nets", "SPI_SCK"] and "![G]ND" in rest and "![V]BAT" in rest
    assert "--power-nets" not in rest and rest[rest.index("--via-size") + 1] == "0.6"


def test_a_route_that_laid_no_track_with_connections_open_fails(board, tmp_path, monkeypatch):
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    for fn in ("_resave", "_edge_band", "_fill"):
        monkeypatch.setattr(krt_route, fn, lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 12, "segments": 0})
    r = krt_route.route(board, work_dir=tmp_path / "w")
    assert not r.ok and "no track segments" in r.reason and r.unrouted == 12
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 0})
    assert krt_route.route(board, work_dir=tmp_path / "w").ok


def test_plane_calls_keep_the_zones_stage6_poured(board):
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    planes = [s["args"] for s in steps if s["script"] == "route_planes.py"]
    assert planes and all("--skip-existing-zones" in a for a in planes)


def test_a_failed_route_still_takes_its_edge_band_off(board, tmp_path, monkeypatch):
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5}}}
    pro.write_text(json.dumps(data))
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    calls = []
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add: calls.append(("band", add)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: subprocess.CompletedProcess(cmd, 1, "", "timed out"))
    r = krt_route.route(board, work_dir=tmp_path / "w")
    assert not r.ok and "exited 1" in r.reason
    assert calls == [("band", True), ("band", False)]


def test_drawing_the_band_first_clears_any_band_left_behind(tmp_path, monkeypatch):
    scripts = []
    monkeypatch.setattr(autoroute, "_pcbnew", lambda py, code: scripts.append(code))
    krt_route._edge_band("py", tmp_path / "b.kicad_pcb", 0.6, add=True)
    krt_route._edge_band("py", tmp_path / "b.kicad_pcb", 0, add=False)
    add, remove = scripts
    for code in scripts:
        compile(code, "<band>", "exec")
    # The add script removes the marked shapes before drawing new ones.
    assert add.index("b.Remove(d)") < add.index("SHAPE_POLY_SET") and "b.Add(sh)" in add
    assert "b.Add" not in remove


def test_a_class_may_mix_pair_conventions():
    assert krt_route._diff_pairs(["USB_DP", "USB_DN", "CLK+", "CLK-"]) == [("CLK+", "CLK-"), ("USB_DP", "USB_DN")]
    assert krt_route._diff_pairs(["USB_DP", "USB_DM", "CLK+", "CLK-"]) == [("CLK+", "CLK-"), ("USB_DP", "USB_DM")]
    # One net without a partner and the class is not a set of pairs.
    assert krt_route._diff_pairs(["USB_DP", "USB_DN", "CLK+"]) == []


@pytest.mark.parametrize("nets, pairs", [
    (["USB_DP", "USB_DM"], [("USB_DP", "USB_DM")]),
    (["MCU_USB_DP", "MCU_USB_DN"], [("MCU_USB_DP", "MCU_USB_DN")]),
    (["ETH_TX_P", "ETH_TX_N", "ETH_RX_P", "ETH_RX_N"], [("ETH_RX_P", "ETH_RX_N"), ("ETH_TX_P", "ETH_TX_N")]),
    (["DSI_CKP", "DSI_CKN"], [("DSI_CKP", "DSI_CKN")]),
    (["LVDS+", "LVDS-"], [("LVDS+", "LVDS-")]),
])
def test_every_pair_stage4_recognises_routes_as_a_pair(nets, pairs):
    # Stage 4 put these in a differential class; the router must agree.
    from blpl.core.stage4_synthesize_nets import _diff_pair_complements

    assert all(any(c in nets for c in _diff_pair_complements(n)) for n in nets)
    assert krt_route._diff_pairs(nets) == pairs


def test_every_copper_layer_is_named_when_there_are_no_planes(board):
    for step in krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]):
        a = step["args"]
        i = a.index("--layers")
        assert a[i + 1:i + 5] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_the_band_runs_round_internal_cutouts_too(tmp_path, monkeypatch):
    # The geometry is checked against pcbnew by hand: a 20 mm board with a
    # 6 mm cutout gets 4 shapes on its edge and 4 round the cutout, all on
    # board material. Here, that the script walks the holes and compiles.
    scripts = []
    monkeypatch.setattr(autoroute, "_pcbnew", lambda py, code: scripts.append(code))
    krt_route._edge_band("py", tmp_path / "b.kicad_pcb", 0.6, add=True)
    compile(scripts[0], "<band>", "exec")
    assert "poly.HoleCount(o)" in scripts[0] and "poly.Hole(o, h)" in scripts[0]


def test_the_plane_skills_point_krt_runs_at_data_it_writes():
    from importlib import resources

    for skill in ("plane-mappings", "erc-drc-resolution"):
        text = (resources.files("blpl") / "skills" / skill / "SKILL.md").read_text()
        assert "kind: plane" in text and "open_connections.json" in text


@pytest.mark.parametrize("net, sel, excl", [
    ("DATA[0]", "DATA[[]0]", "![D]ATA[[]0]"),
    ("A*B", "A[*]B", "![A][*]B"),
    ("Q?", "Q[?]", "![Q][?]"),
    ("!RESET", "\\!RESET", "!!RESET"),
    ("RESET", "RESET", "![R]ESET"),
])
def test_a_net_name_selects_only_itself(net, sel, excl):
    # KRT reads --nets as fnmatch patterns; checked against its
    # expand_net_patterns, each of these selects and excludes itself alone.
    import fnmatch

    assert krt_route._glob(net) == sel and krt_route._not(net) == excl
    others = ["DATA0", "AXB", "QQ", "!RESET", "RESET", "DATA[0]", "A*B", "Q?"]
    assert [n for n in others if fnmatch.fnmatchcase(n, krt_route._pat(net))] == [net]


def test_every_route_selector_is_escaped(tmp_path):
    pcb, pro = _two_class_board(
        tmp_path, ["DATA[0]", "DATA0", "VBAT*", "RF[1]"],
        [{"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
         {"name": "Power_Bulk", "track_width": 0.75, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
         {"name": "RF_50R", "track_width": 0.35, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3}],
        [{"pattern": "VBAT*", "netclass": "Power_Bulk"}, {"pattern": "RF*", "netclass": "RF_50R"}])
    rf, rest = (s["args"] for s in krt_route.plan(pcb, pro, {}, ["F.Cu", "B.Cu"]))
    assert rf[:2] == ["--nets", "RF[[]1]"]
    assert "DATA[[]0]" in rest and "DATA[0]" not in rest and "![R]F[[]1]" in rest
    i = rest.index("--power-nets")
    assert rest[i + 1] == "VBAT[*]"
