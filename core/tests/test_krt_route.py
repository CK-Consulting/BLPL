"""Stage 6a with KiCad Routing Tools (blpl.core.krt_route)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
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
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: clobber())
    monkeypatch.setattr(krt_route, "_snap_terminals", clobber)
    monkeypatch.setattr(krt_route, "_fill", clobber)
    monkeypatch.setattr(krt_route, "_trim_dangling", clobber)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: clobber())
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
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: ok)
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
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add, **kw: calls.append(("band", add, d)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: calls.append(Path(cmd[1]).name) or ok)
    monkeypatch.setattr(krt_route, "_snap_terminals", lambda *a: calls.append("snap") or ok)
    monkeypatch.setattr(krt_route, "_fill", lambda *a: calls.append("fill") or ok)
    monkeypatch.setattr(krt_route, "_trim_dangling", lambda *a: calls.append("trim") or ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: calls.append("stitch") or ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    assert krt_route.route(board, work_dir=tmp_path / "w").ok
    # Band on after the re-save, before any routing; off before the fill.
    # Its depth is the edge rule plus half the widest track (0.75 / 2).
    # Track ends are joined to their pads once the band is off, before the
    # fill; stubs come off after it, when a stub into a pour is seen to
    # connect; ground vias go in last, around the copper that stays.
    assert calls[:2] == ["resave", ("band", True, 0.875)]
    assert calls[-5:] == [("band", False, 0), "snap", "fill", "trim", "stitch"]
    assert all("route" in c for c in calls[2:-5])


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
    for fn in ("_resave", "_edge_band", "_snap_terminals", "_fill", "_trim_dangling", "_stitch"):
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
    for fn in ("_resave", "_edge_band", "_snap_terminals", "_fill", "_trim_dangling", "_stitch"):
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
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add, **kw: calls.append(("band", add)) or ok)
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


def test_signals_are_refused_when_every_copper_layer_is_a_plane(board):
    # GND poured on both sides of a two-layer board: with no --layers the
    # router would fall back to both and route signals through the pours.
    refused = []
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["F.Cu", "B.Cu"]},
                           ["F.Cu", "B.Cu"], refused=refused)
    # The pour, then the route step that welds GND's own pads into it, on
    # GND's layers only; never a signal pass.
    assert [s["script"] for s in steps] == ["route_planes.py", "route.py"]
    weld = steps[1]["args"]
    assert weld[:2] == ["--nets", "GND"] and weld[weld.index("--layers") + 1:][:2] == ["F.Cu", "B.Cu"]
    assert refused[0]["class"] == "*" and refused[0]["nets"] == ["RF_ANT", "SPI_SCK", "VBAT"]
    assert "every copper layer" in refused[0]["reason"]


def test_the_keepout_flag_goes_out_only_with_a_band(board):
    pro = board.with_suffix(".kicad_pro")
    flags = lambda **k: {a for s in krt_route.plan(board, pro, {}, ["F.Cu", "B.Cu"], **k) for a in s["args"]}
    # An edge rule of 0, no band: an Eco2.User drawing must not become a wall.
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0}}}
    pro.write_text(json.dumps(data))
    assert "--keepout" not in flags()
    assert "--keepout" in flags(band=True) and "--keepout" not in flags(band=False)
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5}}}
    pro.write_text(json.dumps(data))
    assert "--keepout" in flags() and "--keepout" not in flags(band=False)


def test_a_band_layer_the_board_already_uses_is_left_alone(board, tmp_path, monkeypatch):
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data["board"] = {"design_settings": {"rules": {"min_copper_edge_clearance": 0.5}}}
    pro.write_text(json.dumps(data))
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    calls, cmds = [], []
    ok = subprocess.CompletedProcess([], 0, "", "")
    taken = subprocess.CompletedProcess([], 3, "1 drawing(s) already on Eco2.User\n", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add, **kw: calls.append(add) or (taken if add else ok))
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: cmds.append(cmd) or ok)
    monkeypatch.setattr(krt_route, "_snap_terminals", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_fill", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_trim_dangling", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 5})
    r = krt_route.route(board, work_dir=tmp_path / "w")
    assert r.ok and calls == [True]                      # drawn once, nothing to remove
    assert cmds and all("--keepout" not in c for c in cmds)
    assert "1 drawing(s) already on Eco2.User" in r.to_dict()["edge_band"]


def test_a_controlled_class_routes_only_on_its_impedance_layers(board):
    # RF_50R's width is for F.Cu: on In2.Cu or B.Cu it is another impedance.
    pro = board.with_suffix(".kicad_pro")
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    steps = krt_route.plan(board, pro, {"GND": ["In1.Cu"]}, layers, class_layers={"RF_50R": ["F.Cu"]})
    (rf,) = [s["args"] for s in steps if s["args"][:2] == ["--nets", "RF_ANT"]]
    assert rf[rf.index("--layers") + 1:] [:1] == ["F.Cu"] and "In2.Cu" not in rf and "B.Cu" not in rf
    # Every other pass keeps every signal layer.
    rest = steps[-1]["args"]
    assert rest[rest.index("--layers") + 1:][:3] == ["F.Cu", "In2.Cu", "B.Cu"]
    # Declared only for a layer that is a plane here: refused, with the reason.
    refused = []
    steps = krt_route.plan(board, pro, {"GND": ["In1.Cu"]}, layers, refused=refused,
                           class_layers={"RF_50R": ["In1.Cu"]})
    assert not any("RF_ANT" in s["args"][:3] for s in steps)
    assert refused[0]["class"] == "RF_50R" and "In1.Cu" in refused[0]["reason"]


def test_impedance_layers_are_read_from_the_hdm():
    hdm = {"net_classes": {"RF_50R": {"impedance": {"ohms": 50, "layers": ["F.Cu"]}},
                           "USB": {"impedance": {"ohms": 90, "mode": "differential"}},
                           "Default": {}}}
    assert krt_route.impedance_layers(hdm) == {"RF_50R": ["F.Cu"]}
    assert krt_route.impedance_layers(None) == {}


def test_stage6a_hands_krt_the_hdms_impedance_layers(tmp_path, monkeypatch):
    import argparse

    from blpl.core import cli

    (tmp_path / "project.yaml").write_text("project: {name: p, dimensions: [10, 10]}\n")
    pipe = tmp_path / ".pipeline"
    pipe.mkdir()
    (pipe / "hdm.yaml").write_text("net_classes:\n  RF_50R:\n    impedance: {ohms: 50, layers: [F.Cu]}\n")
    pcb = pipe / "p.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    seen = {}
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(cli, "_newest_emitted", lambda *a: pcb)
    monkeypatch.setattr(autoroute, "run_for", lambda *a, **k: seen.update(k) or autoroute.RouteResult(
        False, "stub", attempted=False, pcb=str(pcb)))
    assert cli._cmd_autoroute(argparse.Namespace(project_dir=str(tmp_path), board=None, passes=1)) == 0
    assert seen["router"] == "kicad-routing-tools" and seen["class_layers"] == {"RF_50R": ["F.Cu"]}


def test_a_pair_class_named_for_power_is_still_a_pair(tmp_path):
    pcb, pro = _two_class_board(tmp_path, ["USB_DP", "USB_DM", "VBUS"], [
        {"name": "Default", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "LowPower_USB_90R", "track_width": 0.2, "clearance": 0.15, "diff_pair_width": 0.18,
         "diff_pair_gap": 0.15, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Power_Bulk", "track_width": 0.75, "clearance": 0.25, "via_diameter": 0.6, "via_drill": 0.3}],
        [{"pattern": "USB_*", "netclass": "LowPower_USB_90R"}, {"pattern": "VBUS", "netclass": "Power_Bulk"}])
    steps = krt_route.plan(pcb, pro, {}, ["F.Cu", "B.Cu"])
    (diff,) = [s["args"] for s in steps if s["script"] == "route_diff.py"]
    assert diff[:3] == ["--nets", "USB_DM", "USB_DP"] and diff[diff.index("--diff-pair-gap") + 1] == "0.15"
    rest = steps[-1]["args"]
    i = rest.index("--power-nets")
    assert rest[i + 1:i + 2] == ["VBUS"] and "USB_DP" not in rest[i:]


def test_text_on_the_band_layer_is_the_boards_and_has_no_width(tmp_path, monkeypatch):
    # Checked in pcbnew: PCB_TEXT has no GetWidth(), and reading one raised
    # AttributeError before the occupied-layer fallback could run.
    scripts = []
    monkeypatch.setattr(autoroute, "_pcbnew", lambda py, code: scripts.append(code))
    krt_route._edge_band("py", tmp_path / "b.kicad_pcb", 0.6, add=True)
    assert "isinstance(d, pcbnew.PCB_SHAPE) and d.GetWidth()" in scripts[0]


def _fake_home(tmp_path, check: str) -> Path:
    home = tmp_path / "krt"
    (home / "py_router").mkdir(parents=True)
    (home / "rust_router").mkdir()
    (home / "py_router" / "route.py").write_text("")
    (home / "rust_router" / "grid_router.abi3.so").write_text("")
    (home / "py_router" / "startup_checks.py").write_text(check)
    return home


def test_an_install_that_fails_its_own_startup_check_is_not_available(tmp_path, monkeypatch):
    # A stale binary passes a file glob; KRT's startup check is what knows.
    home = _fake_home(tmp_path, "print('Rust router version mismatch: installed=0.22, Cargo.toml=0.23.1')\n"
                                "raise SystemExit(1)\n")
    monkeypatch.setenv("KRT_HOME", str(home))
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(krt_route, "_python_ok", lambda py: True)
    ok, why = krt_route.available()
    assert not ok and "version mismatch" in why
    # And Stage 6a falls back to Freerouting rather than choosing it.
    import argparse

    from blpl.core import cli

    (tmp_path / "project.yaml").write_text("project: {name: p, dimensions: [10, 10]}\n")
    assert cli._router(argparse.Namespace(project_dir=str(tmp_path), board=None), tmp_path) == "freerouting"
    (home / "py_router" / "startup_checks.py").write_text("print('All checks passed.')\n")
    assert krt_route.available() == (True, "")


def test_a_project_file_with_no_edge_or_hole_rule_routes_to_kicads_defaults(board):
    """KiCad's DRC applies its defaults when the .kicad_pro names none, and the
    router's own default edge is 0 mm. On verdacor that put tracks 0.2 mm from
    a 0.5 mm edge and DRC failed on all of them."""
    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data.pop("board", None)
    pro.write_text(json.dumps(data))
    for step in krt_route.plan(board, pro, {}, ["F.Cu", "B.Cu"]):
        a = step["args"]
        assert a[a.index("--board-edge-clearance") + 1] == "0.5"
        assert a[a.index("--hole-to-hole-clearance") + 1] == "0.25"
        assert "--keepout" in a


def test_the_band_rings_npth_holes_at_the_hole_clearance(board, tmp_path, monkeypatch):
    """The router has no copper-to-hole rule; it held 0.21 mm to J_DBG's
    alignment holes where DRC asks 0.25."""
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    seen = []
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda py, pcb, d, add, **k: seen.append((add, d, k)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: ok)
    monkeypatch.setattr(krt_route, "_snap_terminals", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_fill", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_trim_dangling", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    krt_route.route(board, work_dir=tmp_path / "w")
    add, depth, kw = seen[0]
    widest = max(c.get("track_width", 0.2) for c in krt_route._classes(board.with_suffix(".kicad_pro"))[0].values())
    assert add and kw["hole_ring_mm"] == pytest.approx(0.25 + widest / 2)
    assert depth == pytest.approx(0.5 + widest / 2)


def test_the_npth_ring_is_drawn_by_the_band_script(tmp_path, monkeypatch):
    from blpl.core import autoroute as ar

    scripts = []
    monkeypatch.setattr(ar, "_pcbnew", lambda py, s: scripts.append(s) or subprocess.CompletedProcess([], 0, "", ""))
    krt_route._edge_band("py", tmp_path / "b.kicad_pcb", 0.6, add=True, hole_ring_mm=0.35)
    assert "PAD_ATTRIB_NPTH" in scripts[0] and "FromMM(0.35)" in scripts[0]


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_a_track_ending_in_a_chamfered_corner_is_joined_to_its_pad(tmp_path):
    """KRT ends a track on a pad's bounding rectangle; on verdacor's U1 pad 23,
    a chamfered castellation, it ended in the cut-away corner and the net
    stayed open. A track ending on the pad's copper is left alone, and so is
    one whose stub would pass another net or that ends too far off the copper
    to be a near miss (Codex, PR #69)."""
    import pcbnew

    board = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(board, "SIG", 1)
    board.Add(net)
    other = pcbnew.NETINFO_ITEM(board, "OTHER", 2)
    board.Add(other)
    fp = pcbnew.FOOTPRINT(board)
    board.Add(fp)
    mm = pcbnew.FromMM
    for number, x in (("1", 10.0), ("2", 20.0), ("3", 30.0), ("4", 40.0)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CHAMFERED_RECT)
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(1.232), mm(0.95)))
        pad.SetChamferRectRatio(pcbnew.F_Cu, 0.45)
        pad.SetChamferPositions(pcbnew.F_Cu, 4)  # RECT_CHAMFER_BOTTOM_LEFT; not exported by SWIG
        pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(10.0)))
        pad.SetNet(net)
        fp.Add(pad)

    def track(a, b, n=net):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        t.SetWidth(mm(0.2))
        t.SetLayer(pcbnew.F_Cu)
        board.Add(t)
        t.SetNet(n)

    # Into pad 1's cut-away bottom-left corner: inside its box, off its copper.
    track((5.0, 12.0), (9.445, 10.35))
    # Onto pad 2's copper, near its centre.
    track((25.0, 12.0), (20.1, 10.0))
    # The same near miss on pad 3, with another net 0.18 mm from the stub.
    track((25.0, 13.0), (29.445, 10.35))
    track((29.1, 10.2), (29.1, 8.0), other)
    # Into pad 4's very corner: 0.2 mm off its copper, beyond a near miss.
    track((35.0, 12.0), (39.384, 10.475))
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    # pcbnew saves every track of a board built from Python under its first
    # net, whatever the track carries in memory, so the blocker is put on its
    # own net in the file.
    from blpl.emitter import sexpr

    tree = sexpr.parse(pcb.read_text())
    for seg in sexpr.find_all(tree, "segment"):
        name = "OTHER" if sexpr.find(seg, "start")[1:] == ["29.1", "10.2"] else "SIG"
        at = next(i for i, n in enumerate(seg) if isinstance(n, list) and n and n[0] == "net")
        seg[at] = ["net", sexpr.quote(name)]
    pcb.write_text(sexpr.dump_top(tree))
    nets = sorted(t.GetNetname() for t in pcbnew.LoadBoard(str(pcb)).GetTracks())
    assert nets == ["OTHER"] + ["SIG"] * 4

    proc = krt_route._snap_terminals(sys.executable, pcb, 0.2)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "1"
    after = pcbnew.LoadBoard(str(pcb))
    tracks = [t for t in after.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]
    assert len(tracks) == 6

    def ends(t):
        return {(round(pcbnew.ToMM(p.x), 3), round(pcbnew.ToMM(p.y), 3)) for p in (t.GetStart(), t.GetEnd())}

    assert {(9.445, 10.35), (10.0, 10.0)} in [ends(t) for t in tracks]


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_short_dangling_stubs_come_off_and_a_connection_stays(tmp_path):
    """KRT leaves 0.02 to 0.26 mm stubs ending in nothing, each a DRC warning.
    A short one goes, a longer partial route stays, and a track between two
    pads is untouched."""
    import pcbnew

    board = pcbnew.BOARD()
    board.Add(pcbnew.NETINFO_ITEM(board, "SIG"))
    fp = pcbnew.FOOTPRINT(board)
    board.Add(fp)
    mm = pcbnew.FromMM
    for number, x in (("1", 10.0), ("2", 20.0)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(10.0)))
        fp.Add(pad)
        pad.SetNet(board.FindNet("SIG"))

    def track(a, b):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        t.SetWidth(mm(0.2))
        t.SetLayer(pcbnew.F_Cu)
        board.Add(t)
        t.SetNet(board.FindNet("SIG"))

    track((10.0, 10.0), (20.0, 10.0))   # pad to pad: stays
    track((10.0, 10.0), (10.0, 11.2))   # 1.2 mm off pad 1, ends in nothing: a partial route, stays
    track((20.0, 10.0), (20.0, 10.7))   # 0.7 mm off pad 2, ends in nothing: a stub, goes
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    proc = krt_route._trim_dangling(sys.executable, pcb)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "1"
    lengths = sorted(round(pcbnew.ToMM(t.GetLength()), 2) for t in pcbnew.LoadBoard(str(pcb)).GetTracks())
    assert lengths == [1.2, 10.0]


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_ground_vias_go_beside_signal_vias_and_decoupling_pads_where_they_clear(tmp_path):
    """RP-001 and DC-003 on verdacor: 18 signal vias with no ground via within
    1 mm, and U3's decoupling reaching ground only through a long track."""
    import pcbnew

    from blpl.emitter import sexpr

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for name in ("GND", "SIG", "WALL"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(mm(40), mm(30)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(mm(0.1))
    board.Add(edge)
    zone = pcbnew.ZONE(board)
    board.Add(zone)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.In1_Cu)
    layers.AddLayer(pcbnew.In2_Cu)
    zone.SetLayerSet(layers)
    zone.Outline().NewOutline()
    for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
        zone.Outline().Append(mm(x), mm(y))
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("C1")
    board.Add(fp)
    for number, x in (("1", 30.0), ("2", 31.0)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(20.0)))
        fp.Add(pad)

    def via(x, y):
        v = pcbnew.PCB_VIA(board)
        board.Add(v)
        v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)

    def track(a, b, width=0.2):
        t = pcbnew.PCB_TRACK(board)
        board.Add(t)
        t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        t.SetWidth(mm(width))
        t.SetLayer(pcbnew.F_Cu)

    via(10, 10)                       # open: a ground via fits beside it
    track((10, 10), (20, 10))         # its track, which the new via must clear
    via(20, 20)                       # boxed in by another net on F.Cu: no room
    for y in (19.0, 21.0):
        track((18.5, y), (21.5, y), 1.0)
    for x in (19.0, 21.0):
        track((x, 18.5), (x, 21.5), 1.0)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    # Nets set in the file: pcbnew saves a Python-built board's items under
    # its first net whatever they carry in memory.
    tree = sexpr.parse(pcb.read_text())
    top = tree if sexpr.head(tree) == "kicad_pcb" else tree[0]

    def set_net(node, name):
        for i, c in enumerate(node):
            if isinstance(c, list) and sexpr.head(c) == "net":
                node[i] = ["net", sexpr.quote(name)]
                return
        node.append(["net", sexpr.quote(name)])

    for node in top:
        if not isinstance(node, list):
            continue
        tag = sexpr.head(node)
        if tag == "via":
            set_net(node, "SIG")
        elif tag == "segment":
            width = float(sexpr.find(node, "width")[1])
            set_net(node, "WALL" if width > 0.5 else "SIG")
        elif tag == "zone":
            set_net(node, "GND")
        elif tag == "footprint":
            for pad in sexpr.find_all(node, "pad"):
                set_net(pad, "GND" if sexpr.unquote(pad[1]) == "2" else "SIG")
    pcb.write_text(sexpr.dump_top(tree))

    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "2 1"   # the open via and C1, not the boxed one

    after = pcbnew.LoadBoard(str(pcb))
    gnd = [v for v in after.GetTracks() if v.Type() == pcbnew.PCB_VIA_T and v.GetNetname() == "GND"]
    assert len(gnd) == 2
    near = lambda v, x, y: (v.GetPosition() - pcbnew.VECTOR2I(mm(x), mm(y))).EuclideanNorm() <= mm(1.0)
    assert any(near(v, 10, 10) for v in gnd) and any(near(v, 31, 20) for v in gnd)
    assert not any(near(v, 20, 20) for v in gnd)
    sig_track = next(t for t in after.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() == "SIG")
    for v in gnd:
        assert not v.GetEffectiveShape(pcbnew.F_Cu).Collide(sig_track.GetEffectiveShape(pcbnew.F_Cu), mm(0.2))
    link = [t for t in after.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() == "GND"]
    assert len(link) == 1   # C1's pad joined to its via


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_stitching_keeps_each_pads_own_plane_net_every_layer_and_cutout_clear(tmp_path):
    """Codex, PR #72: a VDD pad got a GND link, a through via was checked on
    the outer layers only, and a cutout's edge was not board edge."""
    import pcbnew

    from blpl.emitter import sexpr

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for name in ("GND", "VDD", "SIG", "WALL"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))

    def shape(kind, a, b):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(kind)
        s.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        s.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)

    shape(pcbnew.SHAPE_T_RECTANGLE, (0, 0), (40, 30))
    shape(pcbnew.SHAPE_T_CIRCLE, (31.2, 20), (32.2, 20))      # a 1 mm cutout beside the via at (30, 20)
    for layer in (pcbnew.In1_Cu, pcbnew.In2_Cu):
        zone = pcbnew.ZONE(board)
        board.Add(zone)
        zone.SetLayer(layer)
        zone.Outline().NewOutline()
        for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
            zone.Outline().Append(mm(x), mm(y))
    # GND poured on B.Cu over the right half too, so a ground via there joins
    # two planes: with In1.Cu alone it would join one and float.
    zone = pcbnew.ZONE(board)
    board.Add(zone)
    zone.SetLayer(pcbnew.B_Cu)
    zone.Outline().NewOutline()
    for x, y in ((20, 1), (39, 1), (39, 29), (20, 29)):
        zone.Outline().Append(mm(x), mm(y))
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("C2")
    board.Add(fp)
    for number, x in (("1", 10.0), ("2", 11.0)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(20.0)))
        fp.Add(pad)

    def via(x, y):
        v = pcbnew.PCB_VIA(board)
        board.Add(v)
        v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)

    via(30, 10)                       # walled in on In2 only: no room
    for y in (9.0, 11.0):
        t = pcbnew.PCB_TRACK(board)
        board.Add(t)
        t.SetStart(pcbnew.VECTOR2I(mm(28.5), mm(y)))
        t.SetEnd(pcbnew.VECTOR2I(mm(31.5), mm(y)))
        t.SetWidth(mm(1.0))
        t.SetLayer(pcbnew.In2_Cu)
    for x in (29.0, 31.0):
        t = pcbnew.PCB_TRACK(board)
        board.Add(t)
        t.SetStart(pcbnew.VECTOR2I(mm(x), mm(8.5)))
        t.SetEnd(pcbnew.VECTOR2I(mm(x), mm(11.5)))
        t.SetWidth(mm(1.0))
        t.SetLayer(pcbnew.In2_Cu)
    via(30, 20)                       # beside the cutout: only its far side clears
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    tree = sexpr.parse(pcb.read_text())
    top = tree if sexpr.head(tree) == "kicad_pcb" else tree[0]

    def set_net(node, name):
        for i, c in enumerate(node):
            if isinstance(c, list) and sexpr.head(c) == "net":
                node[i] = ["net", sexpr.quote(name)]
                return
        node.append(["net", sexpr.quote(name)])

    for node in top:
        if not isinstance(node, list):
            continue
        tag = sexpr.head(node)
        if tag == "via":
            set_net(node, "SIG")
        elif tag == "segment":
            set_net(node, "WALL")
        elif tag == "zone":
            set_net(node, "VDD" if sexpr.unquote(sexpr.find(node, "layer")[1]) == "In2.Cu" else "GND")
        elif tag == "footprint":
            for pad in sexpr.find_all(node, "pad"):
                set_net(pad, "VDD" if sexpr.unquote(pad[1]) == "1" else "GND")
    pcb.write_text(sexpr.dump_top(tree))

    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "3 1"

    after = pcbnew.LoadBoard(str(pcb))
    new = [v for v in after.GetTracks() if v.Type() == pcbnew.PCB_VIA_T and v.GetNetname() in ("GND", "VDD")]
    at = lambda v, x, y: (v.GetPosition() - pcbnew.VECTOR2I(mm(x), mm(y))).EuclideanNorm()
    # Each capacitor pad stitched to its own plane net, never the other's.
    assert any(v.GetNetname() == "VDD" and at(v, 10, 20) <= mm(1.0) for v in new)
    assert any(v.GetNetname() == "GND" and at(v, 11, 20) <= mm(1.0) for v in new)
    links = {t.GetNetname() for t in after.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T and t.GetLayer() == pcbnew.F_Cu}
    assert links == {"VDD", "GND"}
    # Nothing through the In2 wall; the cutout via's ground via keeps the edge rule from the cutout.
    assert not any(at(v, 30, 10) <= mm(1.0) for v in new)
    beside_cut = [v for v in new if at(v, 30, 20) <= mm(1.0)]
    assert len(beside_cut) == 1 and beside_cut[0].GetNetname() == "GND"
    assert at(beside_cut[0], 31.2, 20) >= mm(1.0 + 0.5 + 0.3) - 1


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_no_stitching_via_floats_off_a_partial_plane_or_links_across_a_track_keepout(tmp_path):
    """Codex, PR #72: a via outside a partial plane connected nothing, and a
    capacitor's link could cross a keepout that forbids tracks."""
    import pcbnew

    from blpl.emitter import sexpr

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for name in ("GND", "SIG"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(mm(40), mm(30)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(mm(0.1))
    board.Add(edge)
    zone = pcbnew.ZONE(board)                      # GND on In1, left half only
    board.Add(zone)
    zone.SetLayer(pcbnew.In1_Cu)
    zone.Outline().NewOutline()
    for x, y in ((1, 1), (18, 1), (18, 29), (1, 29)):
        zone.Outline().Append(mm(x), mm(y))
    keepout = pcbnew.ZONE(board)                   # no tracks around C3's ground pad
    board.Add(keepout)
    keepout.SetIsRuleArea(True)
    keepout.SetDoNotAllowTracks(True)
    keepout.SetDoNotAllowVias(False)
    keepout.SetDoNotAllowPads(False)
    keepout.SetDoNotAllowFootprints(False)
    keepout.SetDoNotAllowZoneFills(False)
    keepout.SetLayer(pcbnew.F_Cu)
    keepout.Outline().NewOutline()
    for x, y in ((10.6, 18.0), (13.0, 18.0), (13.0, 22.0), (10.6, 22.0)):
        keepout.Outline().Append(mm(x), mm(y))
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("C3")
    board.Add(fp)
    for number, x in (("1", 10.0), ("2", 11.5)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
        pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(20.0)))
        fp.Add(pad)
    via = pcbnew.PCB_VIA(board)                    # outside the plane
    board.Add(via)
    via.SetPosition(pcbnew.VECTOR2I(mm(30), mm(10)))
    via.SetWidth(mm(0.6))
    via.SetDrill(mm(0.3))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    tree = sexpr.parse(pcb.read_text())
    top = tree if sexpr.head(tree) == "kicad_pcb" else tree[0]

    def set_net(node, name):
        for i, c in enumerate(node):
            if isinstance(c, list) and sexpr.head(c) == "net":
                node[i] = ["net", sexpr.quote(name)]
                return
        node.append(["net", sexpr.quote(name)])

    for node in top:
        if not isinstance(node, list):
            continue
        tag = sexpr.head(node)
        if tag == "via":
            set_net(node, "SIG")
        elif tag == "zone" and sexpr.find(node, "keepout") is None:
            set_net(node, "GND")
        elif tag == "footprint":
            for pad in sexpr.find_all(node, "pad"):
                set_net(pad, "GND" if sexpr.unquote(pad[1]) == "2" else "SIG")
    pcb.write_text(sexpr.dump_top(tree))

    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "0 2"
    after = pcbnew.LoadBoard(str(pcb))
    assert not [t for t in after.GetTracks() if t.GetNetname() == "GND"]


# --- via bands beside controlled traces ---------------------------------------


def test_controlled_classes_route_first_and_the_via_bands_follow_them(tmp_path):
    """Once the impedance-controlled nets are down, every later pass routes
    with the bands that keep its vias out of their reference plane."""
    pro = dict(PRO)
    pro["net_settings"] = {**PRO["net_settings"], "classes": [
        *PRO["net_settings"]["classes"],
        {"name": "AA_SPI", "track_width": 0.2, "clearance": 0.15, "via_diameter": 0.6, "via_drill": 0.3}],
        "netclass_patterns": [*PRO["net_settings"]["netclass_patterns"], {"pattern": "SPI_*", "netclass": "AA_SPI"}]}
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD)
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(pro))
    steps = krt_route.plan(pcb, pcb.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], class_layers={"RF_50R": ["F.Cu"]})
    order = [(s["script"], s["args"][1] if s["script"] != krt_route.VIA_BANDS else tuple(s["args"])) for s in steps]
    # Alphabetically AA_SPI would go first and lay its vias where it liked.
    assert order[1:4] == [("route.py", "RF_ANT"), (krt_route.VIA_BANDS, ("RF_ANT",)), ("route.py", "SPI_SCK")]
    # No controlled class, no bands.
    plain = krt_route.plan(pcb, pcb.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]}, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert krt_route.VIA_BANDS not in [s["script"] for s in plain]


def test_a_via_band_covers_the_plane_margin_and_the_hole_a_via_cuts():
    seg = ('(segment (start 0 0) (end 20 0) (width 0.35) (layer "F.Cu") (net "RF_ANT")) '
           '(footprint "J" (at 0 0) (pad "1" smd rect (at 0 0) (size 0.5 0.5) (net "RF_ANT"))) '
           '(footprint "U" (at 20 0) (pad "1" smd rect (at 0 0) (size 0.5 0.5) (net "RF_ANT")))')
    rects = krt_route.via_bands(f"(kicad_pcb {seg})", ["RF_ANT"], via_mm=0.9, clearance_mm=0.25)
    assert rects
    ys = [y for r in rects for _, y in r]
    xs = [x for r in rects for x, _ in r]
    # 0.175 half-width + 3 widths of plane margin + a 0.45 via radius + 0.25.
    assert max(ys) == pytest.approx(0.175 + 3 * 0.35 + 0.45 + 0.25)
    assert min(ys) == pytest.approx(-max(ys))
    # The breakouts stay free: no band within the release's allowance of an end.
    assert min(xs) > 1.5 and max(xs) < 18.5
    assert krt_route.via_bands(f"(kicad_pcb {seg})", ["OTHER"], via_mm=0.9, clearance_mm=0.25) == []


@pytest.fixture
def layered(board):
    """The board with a 4-layer table, so its signals have layers to route on."""
    board.write_text(BOARD.replace("(kicad_pcb", '(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) '
                                   '(6 "In2.Cu" signal) (2 "B.Cu" signal))', 1))
    return board


def test_the_via_bands_come_off_before_the_ground_stitching(layered, tmp_path, monkeypatch):
    board = layered
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {"GND": ["In1.Cu"]})
    calls = []
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "via_bands", lambda text, nets, **k: [[(0, 0), (1, 0), (1, 1)]])
    monkeypatch.setattr(krt_route, "_via_band_areas",
                        lambda py, pcb, rects: calls.append(("bands", rects is not None)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: calls.append(Path(cmd[1]).name) or ok)
    for name in ("_snap_terminals", "_fill", "_trim_dangling"):
        monkeypatch.setattr(krt_route, name, lambda *a, n=name: calls.append(n) or ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: calls.append("stitch") or ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    result = krt_route.route(board, work_dir=tmp_path / "w", class_layers={"RF_50R": ["F.Cu"]})
    assert result.ok and result.extra["via_bands"] == 1
    on, off = calls.index(("bands", True)), calls.index(("bands", False))
    assert calls[on - 1] == "route.py" and "route.py" in calls[on + 1:off]
    assert off < calls.index("stitch")


def test_a_failed_route_still_takes_its_via_bands_off(layered, tmp_path, monkeypatch):
    board = layered
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {"GND": ["In1.Cu"]})
    calls = []
    ok = subprocess.CompletedProcess([], 0, "", "")
    runs = iter([ok, ok, subprocess.CompletedProcess([], 1, "", "boom")])
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "via_bands", lambda text, nets, **k: [[(0, 0), (1, 0), (1, 1)]])
    monkeypatch.setattr(krt_route, "_via_band_areas",
                        lambda py, pcb, rects: calls.append(("bands", rects is not None)) or ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: next(runs, runs_fail))
    runs_fail = subprocess.CompletedProcess([], 1, "", "boom")
    result = krt_route.route(board, work_dir=tmp_path / "w", class_layers={"RF_50R": ["F.Cu"]})
    # Routed protected and then not (the board has an RF net): both fail, and
    # the bands the first put on come off; the second fails before any go on.
    assert not result.ok and calls == [("bands", True), ("bands", False)]
    assert [a["protected"] for a in result.extra["attempts"]] == [True, False]


def test_the_via_band_geometry_goes_in_a_file_not_the_command_line(tmp_path, monkeypatch):
    """2,400 rectangles in the script's source overflowed Linux's limit on one
    argument (Codex, PR #78); the script reads them from a file instead."""
    pcb = tmp_path / "b.kicad_pcb"
    seen = {}

    def fake(python, code, timeout=600):
        seen["code"] = code
        geometry = pcb.with_name(pcb.name + ".via-bands.json")
        seen["rects"] = json.loads(geometry.read_text())
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(autoroute, "_pcbnew", fake)
    rects = [[(float(i), 0.0), (i + 1.0, 0.0), (i + 1.0, 1.0), (float(i), 1.0)] for i in range(2400)]
    assert krt_route._via_band_areas("/usr/bin/python3", pcb, rects).returncode == 0
    assert len(seen["code"]) < 4000 and len(seen["rects"]) == 2400
    assert not pcb.with_name(pcb.name + ".via-bands.json").exists()


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_a_stub_chain_that_needs_a_second_round_comes_off(tmp_path):
    """Removing one stub can leave the next dangling. A second round through
    KiCad's binding failed after its first Remove (a TypeError, then a
    segfault), and pathfinder's core lost its whole route to it; the stubs
    now come out of the file between rounds."""
    import pcbnew

    board = pcbnew.BOARD()
    board.Add(pcbnew.NETINFO_ITEM(board, "SIG"))
    fp = pcbnew.FOOTPRINT(board)
    board.Add(fp)
    mm = pcbnew.FromMM
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(pad.SMDMask())
    pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(1.0), mm(1.0)))
    pad.SetPosition(pcbnew.VECTOR2I(mm(20.0), mm(10.0)))
    fp.Add(pad)
    pad.SetNet(board.FindNet("SIG"))
    for a, b in (((20.0, 10.0), (20.0, 10.6)), ((20.0, 10.6), (20.6, 10.6))):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        t.SetWidth(mm(0.2))
        t.SetLayer(pcbnew.F_Cu)
        board.Add(t)
        t.SetNet(board.FindNet("SIG"))
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    proc = krt_route._trim_dangling(sys.executable, pcb)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "2"
    assert "(segment" not in pcb.read_text()


def _one_plane_board(tmp_path, ground_loop: bool):
    """GND a plane on In1.Cu alone, VDD on In2.Cu, a SIG via at (20, 15); with
    ``ground_loop``, a square of GND track on F.Cu round it, 0.85 mm out, so
    every spot the stitching tries lands partway along one of its sides."""
    import pcbnew

    from blpl.emitter import sexpr

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for name in ("GND", "VDD", "SIG"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    s.SetStart(pcbnew.VECTOR2I(0, 0))
    s.SetEnd(pcbnew.VECTOR2I(mm(40), mm(30)))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(mm(0.1))
    board.Add(s)
    for layer in (pcbnew.In1_Cu, pcbnew.In2_Cu):
        zone = pcbnew.ZONE(board)
        board.Add(zone)
        zone.SetLayer(layer)
        zone.Outline().NewOutline()
        for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
            zone.Outline().Append(mm(x), mm(y))
    v = pcbnew.PCB_VIA(board)
    board.Add(v)
    v.SetPosition(pcbnew.VECTOR2I(mm(20), mm(15)))
    v.SetWidth(mm(0.6))
    v.SetDrill(mm(0.3))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    if ground_loop:
        h = 0.85
        corners = [(20 - h, 15 - h), (20 + h, 15 - h), (20 + h, 15 + h), (20 - h, 15 + h)]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            t = pcbnew.PCB_TRACK(board)
            board.Add(t)
            t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
            t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
            t.SetWidth(mm(0.4))
            t.SetLayer(pcbnew.F_Cu)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    tree = sexpr.parse(pcb.read_text())
    for node in tree:
        if not isinstance(node, list):
            continue
        tag = sexpr.head(node)
        if tag in ("via", "zone", "segment"):
            name = ("SIG" if tag == "via" else "GND" if tag == "segment"
                    else "GND" if sexpr.unquote(sexpr.find(node, "layer")[1]) == "In1.Cu" else "VDD")
            node[:] = [c for c in node if not (isinstance(c, list) and sexpr.head(c) == "net")] + [
                ["net", sexpr.quote(name)]]
    pcb.write_text(sexpr.dump_top(tree))
    return pcb


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_no_stitching_via_where_ground_is_one_plane(tmp_path):
    """On pathfinder's core GND is a plane on In1.Cu alone (In2.Cu is 3V3). A
    ground via beside a signal via there touches one plane and nothing else:
    it connects nothing, and DRC called 130 of them dangling."""
    import pcbnew

    pcb = _one_plane_board(tmp_path, ground_loop=False)
    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "0 1"
    after = pcbnew.LoadBoard(str(pcb))
    assert [t.GetNetname() for t in after.GetTracks() if t.Type() == pcbnew.PCB_VIA_T] == ["SIG"]


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_a_stitching_via_partway_along_a_ground_track_is_kept(tmp_path):
    """A via may land partway along a same-net track, and then it joins the
    track's layer to the plane: two layers, kept. Counting only track ends,
    the check called it one plane and took it out (Codex, PR #83)."""
    import pcbnew

    pcb = _one_plane_board(tmp_path, ground_loop=True)
    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "1 0"
    after = pcbnew.LoadBoard(str(pcb))
    assert sorted(t.GetNetname() for t in after.GetTracks() if t.Type() == pcbnew.PCB_VIA_T) == ["GND", "SIG"]


def test_search_settings_go_on_every_track_call_and_never_the_planes(board):
    search = {"ordering": "inside_out", "mps_reverse_rounds": True, "max_ripup": 5,
              "heuristic_weight": 1.5, "direction": None}
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], search=search)
    tail = ["--ordering", "inside_out", "--mps-reverse-rounds", "--max-ripup", "5", "--heuristic-weight", "1.5"]
    for s in steps:
        if s["script"] == "route_planes.py":
            assert "--ordering" not in s["args"]
        else:
            assert s["args"][-len(tail):] == tail, s
    # Without settings the calls are exactly what they were.
    plain = krt_route.plan(board, board.with_suffix(".kicad_pro"), {"GND": ["In1.Cu"]},
                           ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert [s["args"] for s in plain] == [s["args"][:len(s["args"]) - (0 if s["script"] == "route_planes.py" else len(tail))]
                                          for s in steps]


def test_a_misspelt_search_setting_is_an_error_not_a_default_route(board):
    with pytest.raises(ValueError, match="unknown setting.*orderring"):
        krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, ["F.Cu", "B.Cu"], search={"orderring": "mps"})


def test_the_schema_and_the_router_flags_name_the_same_settings():
    from blpl.core import schema

    defs = json.loads((Path(krt_route.__file__).parents[2] / "schemas" / "project_config.v1.json").read_text())
    assert set(defs["$defs"]["krt_search"]["properties"]) == set(krt_route.SEARCH_FLAGS)
    base = {"project": {"name": "p", "dimensions": [10, 10]}}
    schema.validate("project_config", {**base, "routing": {
        "search": {"ordering": "bus", "max_ripup": 4},
        "retry": [{"ordering": "inside_out"}, {"direction": "backward", "mps_reverse_rounds": True}]}})
    for bad in ({"search": {"ordering": "random"}}, {"retry": [{"seed": 3}]}, {"search": {"track_width": 0.1}}):
        with pytest.raises(Exception):
            schema.validate("project_config", {**base, "routing": bad})


def _attempts(monkeypatch, board, opens):
    """_route_once stubbed: attempt i finds the board as it was before routing,
    writes its own copper, and leaves opens[i] connections open (None: fails)."""
    original = board.read_text()
    calls = []
    fanouts = monkeypatch.fanouts = []

    def once(pcb, *, work_dir, timeout, class_layers, search, fanout=None, protect=True):
        i = len(calls)
        calls.append((search, pcb.read_text() == original))
        fanouts.append(fanout)
        pcb.write_text(f"routed by attempt {i}")
        if opens[i] is None:
            return autoroute.RouteResult(False, f"route.py exited 1 on attempt {i}", pcb=str(pcb))
        return autoroute.RouteResult(True, "", pcb=str(pcb), unrouted=opens[i], extra={"router": "kicad-routing-tools"})

    monkeypatch.setattr(krt_route, "_route_once", once)
    return calls


def test_retries_start_from_the_unrouted_board_and_stop_at_the_first_clean_route(board, tmp_path, monkeypatch):
    calls = _attempts(monkeypatch, board, [2, None, 0, 0])
    r = krt_route.route(board, work_dir=tmp_path / "w", search={"max_ripup": 4},
                        retry=[{"ordering": "inside_out"}, {"direction": "backward"}, {"ordering": "bus"}])
    assert r.ok and r.unrouted == 0 and r.extra["kept_attempt"] == 2
    assert board.read_text() == "routed by attempt 2"
    # Each retry is laid over the base settings, on the board as it was.
    assert calls == [({"max_ripup": 4}, True), ({"max_ripup": 4, "ordering": "inside_out"}, True),
                     ({"max_ripup": 4, "direction": "backward"}, True)]
    assert [(a["ok"], a["unrouted"]) for a in r.extra["attempts"]] == [(True, 2), (False, None), (True, 0)]
    assert "exited 1" in r.extra["attempts"][1]["reason"]


def test_with_no_clean_route_the_fewest_open_is_kept_the_earliest_on_a_tie(board, tmp_path, monkeypatch):
    _attempts(monkeypatch, board, [3, 1, 2, 1])
    r = krt_route.route(board, work_dir=tmp_path / "w", retry=[{"ordering": "bus"}, {"ordering": "original"},
                                                              {"direction": "backward"}])
    assert r.ok and r.unrouted == 1 and r.extra["kept_attempt"] == 1
    assert board.read_text() == "routed by attempt 1" and len(r.extra["attempts"]) == 4


def test_without_retries_the_board_is_routed_once(board, tmp_path, monkeypatch):
    calls = _attempts(monkeypatch, board, [2])
    r = krt_route.route(board, work_dir=tmp_path / "w", search={"ordering": "bus"})
    assert r.unrouted == 2 and "attempts" not in r.extra and calls == [({"ordering": "bus"}, True)]


def test_retries_stop_when_the_router_is_not_there_to_try(board, tmp_path, monkeypatch):
    monkeypatch.setattr(krt_route, "available", lambda: (False, "KiCad Routing Tools is not installed"))
    r = krt_route.route(board, work_dir=tmp_path / "w", retry=[{"ordering": "bus"}, {"ordering": "original"}])
    assert not r.attempted and "not installed" in r.reason


@pytest.mark.parametrize("router, handed", [("kicad-routing-tools", True), ("freerouting", False)])
def test_stage6a_hands_krt_the_projects_search_and_retries(tmp_path, monkeypatch, capsys, router, handed):
    import argparse

    from blpl.core import cli

    (tmp_path / "project.yaml").write_text(
        "project: {name: p, dimensions: [10, 10]}\n"
        f"routing: {{router: {router}, search: {{ordering: bus}}, retry: [{{direction: backward}}]}}\n")
    pipe = tmp_path / ".pipeline"
    pipe.mkdir()
    pcb = pipe / "p.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    seen = {}
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(cli, "_newest_emitted", lambda *a: pcb)
    monkeypatch.setattr(autoroute, "run_for", lambda *a, **k: seen.update(k) or autoroute.RouteResult(
        False, "stub", attempted=False, pcb=str(pcb)))
    assert cli._cmd_autoroute(argparse.Namespace(project_dir=str(tmp_path), board=None, passes=1)) == 0
    if handed:
        assert seen["search"] == {"ordering": "bus"} and seen["retry"] == [{"direction": "backward"}]
    else:
        # Freerouting takes none of it, and the run says so.
        assert seen["search"] is None and seen["retry"] is None
        assert "freerouting does not take them" in capsys.readouterr().out


@pytest.mark.parametrize("search, retry", [({"orderring": "mps"}, None), (None, [{"ordering": "bus"}, {"seed": 3}])])
def test_a_misspelt_setting_is_refused_before_the_board_is_touched(board, tmp_path, monkeypatch, search, retry):
    before = board.read_text()
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    touched = []
    monkeypatch.setattr(krt_route, "_resave", lambda *a: touched.append("resave"))
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: touched.append("band"))
    r = autoroute.run_for(board, tmp_path / "r.json", router="kicad-routing-tools", search=search, retry=retry)
    assert not r.ok and r.attempted and "unknown setting" in r.reason
    assert touched == [] and board.read_text() == before
    # And the report is written, saying why.
    assert "unknown setting" in json.loads((tmp_path / "r.json").read_text())["reason"]


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_no_stitching_via_lands_in_a_keepout_a_footprint_carries(tmp_path):
    """A module's antenna keep-out is a rule area in its footprint, not in
    b.Zones(), and a ground via went into the BM20BC's (verdacor-ear-tag-bm20)."""
    import pcbnew

    from blpl.emitter import sexpr

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for name in ("GND", "SIG"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(mm(40), mm(30)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(mm(0.1))
    board.Add(edge)
    zone = pcbnew.ZONE(board)                      # GND over both inner layers
    board.Add(zone)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.In1_Cu)
    layers.AddLayer(pcbnew.In2_Cu)
    zone.SetLayerSet(layers)
    zone.Outline().NewOutline()
    for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
        zone.Outline().Append(mm(x), mm(y))
    fp = pcbnew.FOOTPRINT(board)                   # a module whose keep-out
    fp.SetReference("U1")                          # covers the signal via
    board.Add(fp)
    keepout = pcbnew.ZONE(fp)
    keepout.SetIsRuleArea(True)
    keepout.SetDoNotAllowVias(True)
    keepout.SetDoNotAllowTracks(True)
    keepout.SetDoNotAllowPads(False)
    keepout.SetDoNotAllowFootprints(False)
    keepout.SetDoNotAllowZoneFills(False)          # vias out, the plane still poured
    keepout.SetLayerSet(pcbnew.LSET.AllCuMask())
    keepout.Outline().NewOutline()
    for x, y in ((14, 9), (26, 9), (26, 21), (14, 21)):
        keepout.Outline().Append(mm(x), mm(y))
    fp.Add(keepout)
    via = pcbnew.PCB_VIA(board)                    # a signal via just left of the keep-out:
    # the stitcher tries the +x spot first, which is inside it
    board.Add(via)
    via.SetPosition(pcbnew.VECTOR2I(mm(13.4), mm(15)))
    via.SetWidth(mm(0.6))
    via.SetDrill(mm(0.3))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)

    tree = sexpr.parse(pcb.read_text())
    top = tree if sexpr.head(tree) == "kicad_pcb" else tree[0]
    for node in top:
        if isinstance(node, list) and sexpr.head(node) in ("via", "zone") and sexpr.find(node, "keepout") is None:
            name = "SIG" if sexpr.head(node) == "via" else "GND"
            node[:] = [c for c in node if not (isinstance(c, list) and sexpr.head(c) == "net")] + [["net", sexpr.quote(name)]]
    pcb.write_text(sexpr.dump_top(tree))
    assert pcbnew.LoadBoard(str(pcb)).GetFootprints()[0].Zones(), "the keep-out must be the footprint's"

    proc = krt_route._stitch(sys.executable, pcb, via_mm=0.6, drill_mm=0.3, clearance_mm=0.2,
                             edge_mm=0.5, hole_mm=0.25, hole_to_hole_mm=0.25)
    assert proc.returncode == 0, proc.stderr
    # One placed, so the check below is about where, not whether.
    assert proc.stdout.strip().splitlines()[-1] == "1 0"
    after = pcbnew.LoadBoard(str(pcb))
    area = after.GetFootprints()[0].Zones()[0].Outline()
    inside = [v for v in after.GetTracks() if v.Type() == pcbnew.PCB_VIA_T
              and v.GetNetname() == "GND" and area.Contains(v.GetPosition())]
    assert not inside


def _with_parts(board, *refs):
    """The board with a footprint for each reference designator."""
    parts = " ".join(f'(footprint "X" (property "Reference" "{r}"))' for r in refs)
    text = board.read_text().rstrip()
    board.write_text(text[:-1] + f" {parts})")  # inside the board's own closing paren
    return board


def test_a_fanout_comes_first_at_the_default_class_and_off_the_planes(layered):
    board = _with_parts(layered, "U1", "U2")
    pro = board.with_suffix(".kicad_pro")
    steps = krt_route.plan(board, pro, {"GND": ["In1.Cu"]},
                           fanout=[{"ref": "U1"}, {"ref": "U2", "escape": "channel"}])
    assert [s["script"] for s in steps[:3]] == ["bga_fanout.py", "bga_fanout.py", "route_planes.py"]
    u1, u2 = steps[0]["args"], steps[1]["args"]
    arg = lambda a, flag: a[a.index(flag) + 1]
    assert arg(u1, "--component") == "U1" and arg(u2, "--component") == "U2"
    # Between the pads unless the project says otherwise: a via in a pad is
    # a fab process (filled and capped), not a default.
    assert arg(u1, "--escape-method") == "dogbone" and arg(u2, "--escape-method") == "channel"
    # Default's sizes, not GND's Power_Bulk: a 0.75 mm track does not fit
    # between the pads it is escaping from.
    assert (arg(u1, "--track-width"), arg(u1, "--clearance"),
            arg(u1, "--via-size"), arg(u1, "--via-drill")) == ("0.2", "0.15", "0.6", "0.3")
    layers = u1[u1.index("--layers") + 1:u1.index("--escalation")]
    assert layers == ["F.Cu", "In2.Cu", "B.Cu"]
    assert arg(u1, "--escalation") == "off" and "--no-fix-drc-settings" in u1


def test_a_fanout_for_a_part_not_on_the_board_is_refused_and_the_rest_routes(layered):
    board = _with_parts(layered, "U1")
    refused = []
    steps = krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, refused=refused,
                           fanout=[{"ref": "U9"}, {"ref": "U1"}])
    assert [s["args"][1] for s in steps if s["script"] == "bga_fanout.py"] == ["U1"]
    assert refused == [{"fanout": "U9", "reason": "no footprint with this reference on the board"}]
    # An empty list is no fan-out at all.
    assert not [s for s in krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, fanout=[])
                if s["script"] == "bga_fanout.py"]


def test_the_fanout_writes_back_to_the_board_it_reads(layered, tmp_path, monkeypatch):
    board = _with_parts(layered, "U1")
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    cmds = []
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(krt_route, "_resave", lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: cmds.append(cmd) or ok)
    for name in ("_snap_terminals", "_fill", "_trim_dangling"):
        monkeypatch.setattr(krt_route, name, lambda *a: ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    krt_route.route(board, work_dir=tmp_path / "w", fanout=[{"ref": "U1"}])
    fan = next(c for c in cmds if Path(c[1]).name == "bga_fanout.py")
    # bga_fanout.py takes -o/--output; route.py's -O is not one of its flags.
    assert fan[2:5] == [str(board), "--output", str(board)]
    assert Path(cmds[0][1]).name == "bga_fanout.py"


def test_every_retry_fans_out_again(board, tmp_path, monkeypatch):
    """Each retry starts from the unrouted board, so it has no escapes on it."""
    _attempts(monkeypatch, board, [2, 1, 0])
    fan = [{"ref": "U1"}]
    krt_route.route(board, work_dir=tmp_path / "w", fanout=fan, retry=[{"ordering": "bus"}, {"ordering": "original"}])
    assert monkeypatch.fanouts == [fan, fan, fan]


def test_the_schema_takes_a_fanout_list_and_nothing_else_in_it():
    from blpl.core import schema

    base = {"project": {"name": "p", "dimensions": [10, 10]}}
    schema.validate("project_config", {**base, "routing": {"fanout": [{"ref": "U1"}, {"ref": "U2", "escape": "channel"}]}})
    for bad in ([{"escape": "dogbone"}], [{"ref": "U1", "escape": "sideways"}], [{"ref": "U1", "width": 0.1}], {"ref": "U1"}):
        with pytest.raises(jsonschema.ValidationError):
            schema.validate("project_config", {**base, "routing": {"fanout": bad}})


@pytest.mark.parametrize("fanout", [{"ref": "U1"}, [{"escape": "dogbone"}], [{"ref": ""}], ["U1"],
                                    [{"ref": "U1", "escape": "sideways"}], [{"ref": "U1", "width": 0.1}]])
def test_a_malformed_fanout_is_refused_before_the_board_is_touched(board, tmp_path, monkeypatch, fanout):
    """project.yaml reaches Stage 6a unvalidated (Codex, PR #104)."""
    touched = []
    monkeypatch.setattr(krt_route, "_route_once", lambda *a, **k: touched.append(1))
    before = board.read_text()
    r = krt_route.route(board, work_dir=tmp_path / "w", fanout=fanout)
    assert not r.ok and "routing.fanout" in r.reason and not touched and board.read_text() == before


def _footprint_keepout(pcbnew, fp, box, *, vias, tracks):
    """A rule area on every copper layer, owned by ``fp`` (not the board)."""
    mm = pcbnew.FromMM
    z = pcbnew.ZONE(fp)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowVias(vias)
    z.SetDoNotAllowTracks(tracks)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowFootprints(False)
    z.SetDoNotAllowZoneFills(False)
    z.SetLayerSet(pcbnew.LSET.AllCuMask())
    z.Outline().NewOutline()
    (x0, y0), (x1, y1) = box
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        z.Outline().Append(mm(x), mm(y))
    fp.Add(z)


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_a_near_miss_is_not_joined_across_a_track_keepout_a_footprint_carries(tmp_path):
    """_snap_terminals read only b.Zones(), so a footprint's track keep-out
    over the join was not seen (Codex, PR #105)."""
    import pcbnew

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(board, "SIG", 1)
    board.Add(net)
    fp = pcbnew.FOOTPRINT(board)
    board.Add(fp)
    pad = pcbnew.PAD(fp)                           # pad 1 of the chamfered-corner test
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(pad.SMDMask())
    pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CHAMFERED_RECT)
    pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(1.232), mm(0.95)))
    pad.SetChamferRectRatio(pcbnew.F_Cu, 0.45)
    pad.SetChamferPositions(pcbnew.F_Cu, 4)
    pad.SetPosition(pcbnew.VECTOR2I(mm(10.0), mm(10.0)))
    pad.SetNet(net)
    fp.Add(pad)
    # The stub the snap would draw runs from (9.445, 10.35) to the pad centre.
    _footprint_keepout(pcbnew, fp, ((9.3, 9.8), (10.2, 10.6)), vias=False, tracks=True)
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(mm(5.0), mm(12.0)))
    t.SetEnd(pcbnew.VECTOR2I(mm(9.445), mm(10.35)))
    t.SetWidth(mm(0.2))
    t.SetLayer(pcbnew.F_Cu)
    board.Add(t)
    t.SetNet(net)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    assert pcbnew.LoadBoard(str(pcb)).GetFootprints()[0].Zones(), "the keep-out must be the footprint's"

    proc = krt_route._snap_terminals(sys.executable, pcb, 0.2)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "0"
    assert len([t for t in pcbnew.LoadBoard(str(pcb)).GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]) == 1


@pytest.mark.skipif(shutil.which("kicad-cli") is None or subprocess.run(
    [sys.executable, "-c", "import pcbnew"], capture_output=True).returncode != 0,
    reason="needs KiCad's pcbnew")
def test_freeroutings_plane_fanout_keeps_its_via_out_of_a_footprints_keepout(tmp_path):
    """plane_fanout.py, Freerouting's path, read only b.Zones() too (Codex, PR #105)."""
    import pcbnew

    from blpl.core import plane_fanout

    mm = pcbnew.FromMM
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    net = pcbnew.NETINFO_ITEM(board, "GND", 1)
    board.Add(net)
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    edge.SetStart(pcbnew.VECTOR2I(0, 0))
    edge.SetEnd(pcbnew.VECTOR2I(mm(20), mm(20)))
    edge.SetLayer(pcbnew.Edge_Cuts)
    edge.SetWidth(mm(0.1))
    board.Add(edge)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(pad.SMDMask())
    pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
    pad.SetPosition(pcbnew.VECTOR2I(mm(10.0), mm(10.0)))
    pad.SetNet(net)
    fp.Add(pad)
    # Below the pad, the first spot the fan-out tries.
    _footprint_keepout(pcbnew, fp, ((8.0, 10.4), (12.0, 13.0)), vias=True, tracks=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    assert pcbnew.LoadBoard(str(pcb)).GetFootprints()[0].Zones(), "the keep-out must be the footprint's"

    proc = autoroute._pcbnew(sys.executable, plane_fanout.script(str(pcb), {"GND": ["In1.Cu"]}))
    assert proc.returncode == 0, proc.stderr
    assert plane_fanout.parse(proc.stdout)["placed"] == [["U1", "1", "GND"]]
    vias = [v for v in pcbnew.LoadBoard(str(pcb)).GetTracks() if v.Type() == pcbnew.PCB_VIA_T]
    assert len(vias) == 1 and pcbnew.ToMM(vias[0].GetPosition().y) < 10.4



def _with_pads(board, parts):
    """The board with footprints that have pads: {ref: (rows, cols, net_of)}.

    ``net_of(r, c)`` names each pad's net; rows x cols pads on a 1 mm grid."""
    fps = []
    for ref, (rows, cols, net_of) in parts.items():
        pads = " ".join(f'(pad "{r * cols + c + 1}" smd circle (at {c} {r}) (size 0.4 0.4) (net "{net_of(r, c)}"))'
                        for r in range(rows) for c in range(cols))
        fps.append(f'(footprint "X" (at 10 10 90) (property "Reference" "{ref}") {pads})')
    text = board.read_text().rstrip()
    board.write_text(text[:-1] + " " + " ".join(fps) + ")")
    return board


def test_with_no_fanout_set_every_u_part_whose_pads_fill_a_grid_is_fanned_out(layered):
    """The default: ICs with pads inside the grid, each optional. A part with
    its pads on its outline is left alone (verdacor-ear-tag-bm20: every U
    part fanned out went from 6 open to 15, with a short)."""
    sig = lambda r, c: "SPI_SCK"
    board = _with_pads(layered, {"U1": (3, 3, sig), "U_MCU": (4, 4, sig),
                                 "U2": (2, 5, sig),       # two rows: a SOIC, a module's edge
                                 "R1": (3, 3, sig)})      # a grid, but not a U part
    steps = [s for s in krt_route.plan(board, board.with_suffix(".kicad_pro"), {}) if s["script"] == "bga_fanout.py"]
    refs = [s["args"][s["args"].index("--component") + 1] for s in steps]
    assert refs == ["U1", "U_MCU"]
    assert all(s["optional"] for s in steps)
    assert all(s["args"][s["args"].index("--escape-method") + 1] == "dogbone" for s in steps)
    # Named in project.yaml, a part is fanned out grid or not, and not optional.
    named = [s for s in krt_route.plan(board, board.with_suffix(".kicad_pro"), {}, fanout=[{"ref": "U2"}])
             if s["script"] == "bga_fanout.py"]
    assert len(named) == 1 and not named[0]["optional"]


def test_no_fanout_escapes_an_impedance_controlled_net(layered):
    """A stub and a via on a 50 ohm feed is what the class exists to avoid."""
    board = _with_pads(layered, {"U1": (3, 3, lambda r, c: "RF_ANT" if (r, c) == (0, 0) else "SPI_SCK"),
                                 "U9": (3, 3, lambda r, c: "RF_ANT")})
    pro = board.with_suffix(".kicad_pro")
    steps = [s for s in krt_route.plan(board, pro, {}, class_layers={"RF_50R": ["F.Cu"]})
             if s["script"] == "bga_fanout.py"]
    # U1 fans out its other nets only; U9, all RF, not at all.
    assert [s["args"][s["args"].index("--component") + 1] for s in steps] == ["U1"]
    args = steps[0]["args"]
    assert args[args.index("--nets") + 1:args.index("--escalation")] == ["SPI_SCK"]
    # Without a controlled class on the part, no --nets: every pad escapes.
    plain = [s for s in krt_route.plan(board, pro, {}) if s["script"] == "bga_fanout.py"]
    assert all("--nets" not in s["args"] for s in plain)


def test_a_defaulted_fanout_that_fails_leaves_the_board_as_it_was_and_the_route_goes_on(layered, tmp_path, monkeypatch):
    board = _with_pads(layered, {"U1": (3, 3, lambda r, c: "SPI_SCK"), "U2": (3, 3, lambda r, c: "SPI_SCK")})
    monkeypatch.setattr(krt_route, "available", lambda: (True, ""))
    monkeypatch.setattr(krt_route, "find_home", lambda: tmp_path)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "plane_layers", lambda pcb: {})
    ok = subprocess.CompletedProcess([], 0, "", "")
    for name in ("_resave", "_snap_terminals", "_fill", "_trim_dangling"):
        monkeypatch.setattr(krt_route, name, lambda *a: ok)
    monkeypatch.setattr(krt_route, "_edge_band", lambda *a, **k: ok)
    monkeypatch.setattr(krt_route, "_stitch", lambda *a, **k: ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 0, "segments": 1})
    before = board.read_text()
    ran = []

    def run(cmd, timeout):
        ran.append(Path(cmd[1]).name)
        if Path(cmd[1]).name == "bga_fanout.py" and "U1" in cmd:
            board.write_text("half-written by a fan-out that then failed")
            return subprocess.CompletedProcess(cmd, 2, "", "not a grid")
        return ok

    monkeypatch.setattr(krt_route, "_run", run)
    result = krt_route.route(board, work_dir=tmp_path / "w")
    assert result.ok
    assert result.extra["fanout_skipped"][0]["ref"] == "U1" and "not a grid" in result.extra["fanout_skipped"][0]["reason"]
    # U1's half-done work is gone; U2 still fanned out, and the routing ran.
    assert ran.count("bga_fanout.py") == 2 and "route.py" in ran
    assert "half-written" not in board.read_text() and board.read_text() == before


def test_a_qfns_exposed_pad_and_its_thermal_vias_do_not_make_it_a_grid():
    """Eight pins round a big centre pad is a QFN, not a grid (Codex, PR #106)."""
    ring = [(x, y, "SIG", 0.3, 0.8, True) for x, y in
            ((-1, -2), (0, -2), (1, -2), (-1, 2), (0, 2), (1, 2), (-2, 0), (2, 0))]
    ep = [(0, 0, "GND", 2.0, 2.0, True)]
    assert not krt_route.is_grid(ring + ep)
    # Thermal vias in the exposed pad: small, inside it, and through-hole.
    vias = [(x, y, "GND", 0.4, 0.4, False) for x in (-0.5, 0.5) for y in (-0.5, 0.5)]
    assert not krt_route.is_grid(ring + ep + vias)
    # Even small surface pads, if they sit inside the exposed pad.
    paste_split = [(x, y, "GND", 0.4, 0.4, True) for x in (-0.5, 0.5) for y in (-0.5, 0.5)]
    assert not krt_route.is_grid(ring + ep + paste_split)
    # A 3 x 3 BGA: its middle ball is a pad like the others, so it is a grid.
    bga = [(x, y, "SIG", 0.4, 0.4, True) for x in (-1, 0, 1) for y in (-1, 0, 1)]
    assert krt_route.is_grid(bga)


def test_footprint_pads_reads_size_kind_and_skips_paste_apertures():
    text = ('(kicad_pcb (footprint "X" (at 0 0) (property "Reference" "U1")'
            ' (pad "1" smd rect (at 1 2) (size 0.3 0.8) (layers "F.Cu" "F.Paste") (net "A"))'
            ' (pad "" smd rect (at 0 0) (size 1 1) (layers "F.Paste"))'
            ' (pad "2" thru_hole circle (at 0 0) (size 0.5 0.5) (drill 0.3) (layers "*.Cu") (net "GND"))))')
    assert krt_route.footprint_pads(text) == {"U1": [(1.0, 2.0, "A", 0.3, 0.8, True),
                                                     (0.0, 0.0, "GND", 0.5, 0.5, False)]}


# --- the layer guard: a restricted class's copper checked on what shipped ---

RF_ONLY_F = {"RF_50R": ["F.Cu"]}


def _routed(*items: str) -> str:
    return ('(kicad_pcb (net 0 "") (net 1 "GND") (net 2 "VBAT") (net 3 "RF_ANT") (net 4 "SPI_SCK")\n'
            + "\n".join(items) + ")\n")


def _seg(net: str, layer: str, x1: float = 5, x0: float = 0) -> str:
    return f'(segment (start {x0} 0) (end {x1} 0) (width 0.35) (layer "{layer}") (net "{net}"))'


def _via(net: str, a: str = "F.Cu", b: str = "B.Cu") -> str:
    return f'(via (at 1 1) (size 0.6) (drill 0.3) (layers "{a}" "{b}") (net "{net}"))'


def test_off_layer_tracks_and_vias_on_a_restricted_net_are_found_in_both_net_spellings(board):
    board.write_text(_routed(
        _seg("RF_ANT", "F.Cu"),                      # where it belongs
        _seg("RF_ANT", "B.Cu", x1=3),                # off, KiCad 10's spelling
        '(segment (start 0 0) (end 0 4) (width 0.35) (layer "B.Cu") (net 3))',  # off, the older one
        _via("RF_ANT"),                              # leaves F.Cu: off by being there
        _seg("SPI_SCK", "B.Cu"), _via("SPI_SCK")))   # not restricted: none of the guard's business
    assert krt_route.layer_violations(board, RF_ONLY_F) == [
        {"net": "RF_ANT", "class": "RF_50R", "allowed": ["F.Cu"], "tracks_mm": {"B.Cu": 7.0}, "vias": 1}]
    # Nothing restricted, nothing to report -- and a clean net is clean.
    assert krt_route.layer_violations(board, None) == []
    board.write_text(_routed(_seg("RF_ANT", "F.Cu"), _seg("SPI_SCK", "B.Cu")))
    assert krt_route.layer_violations(board, RF_ONLY_F) == []


def test_a_via_is_judged_by_both_its_ends(board):
    board.write_text(_routed(_via("RF_ANT"), _via("RF_ANT", "F.Cu", "In1.Cu")))
    found = krt_route.layer_violations(board, {"RF_50R": ["F.Cu", "B.Cu"]})
    # The through via changes layers the class allows; the blind one lands on In1.Cu.
    assert [e["vias"] for e in found] == [1]


def _layered_attempts(monkeypatch, boards: list[str], opens: list[int]):
    """_route_once stubbed: attempt i writes boards[i] and leaves opens[i] open."""
    def once(pcb, *, work_dir, timeout, class_layers, search, fanout=None, protect=True):
        i = once.calls
        once.calls += 1
        once.protects.append(protect)
        pcb.write_text(boards[i])
        return autoroute.RouteResult(True, "", pcb=str(pcb), unrouted=opens[i], extra={"router": "kicad-routing-tools"})

    once.calls = 0
    once.protects = []
    monkeypatch.setattr(krt_route, "_route_once", once)
    return once


def test_an_attempt_with_a_restricted_net_off_its_layer_loses_to_a_clean_one_with_more_open(board, tmp_path, monkeypatch):
    """ear-tag-bm20 kept the attempt with 9 open over the one with 10; the 9
    had its 2.4 GHz feed on B.Cu. Opens are compared only among the clean."""
    clean = _routed(_seg("RF_ANT", "F.Cu"))
    stray = _routed(_seg("RF_ANT", "F.Cu"), _seg("RF_ANT", "B.Cu"), _via("RF_ANT"))
    once = _layered_attempts(monkeypatch, [clean, stray, stray, stray], [3, 1, 0, 0])
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F,
                        retry=[{"direction": "backward"}])
    # A violating route with nothing open does not end the retries early...
    assert once.calls == 4
    # ...and does not win: the clean attempt is kept, as routed.
    assert r.ok and r.extra["kept_attempt"] == 0 and r.unrouted == 3
    assert board.read_text() == clean
    assert r.extra["layer_violations"] == [] and r.extra["layer_checked_nets"] == 1
    assert "layer_warning" not in r.extra
    assert [a.get("layer_violations") for a in r.extra["attempts"]] == [None, ["RF_ANT"], ["RF_ANT"], ["RF_ANT"]]


def test_when_every_attempt_strays_the_copper_is_stripped_and_the_report_says_so(board, tmp_path, monkeypatch):
    def stray(extra: str) -> str:
        return _routed(_seg("RF_ANT", "F.Cu"), _seg("RF_ANT", "B.Cu", x1=4), _via("RF_ANT"),
                       _seg("SPI_SCK", "B.Cu"), extra)

    worse, better = stray(_seg("GND", "F.Cu")), stray(_seg("VBAT", "F.Cu"))
    _layered_attempts(monkeypatch, [worse, better, worse, worse], [5, 2, 5, 5])
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    ok = subprocess.CompletedProcess([], 0, "", "")
    filled = []
    monkeypatch.setattr(krt_route, "_fill", lambda py, pcb: filled.append(pcb.read_text()) or ok)
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 3, "segments": 3})
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F, retry=[{"direction": "backward"}])

    # The best of the violators is kept: fewest open among them.
    assert r.ok and r.extra["kept_attempt"] == 1
    # Its off-layer copper is gone and only that: the F.Cu run, the other
    # nets' copper and the attempt's own work stay.
    left = board.read_text()
    assert '"RF_ANT"' in left and krt_route.layer_violations(board, RF_ONLY_F) == []
    assert left.count("(segment") == 3 and "(via" not in left and '(net "VBAT")' in left
    # The pour is refilled around what is left, and the open count re-measured.
    assert filled and r.unrouted == 3
    # Loud: the violation, the strip, and the board as the router left it.
    assert r.extra["layer_violations"] == [
        {"net": "RF_ANT", "class": "RF_50R", "allowed": ["F.Cu"], "tracks_mm": {"B.Cu": 4.0}, "vias": 1}]
    assert r.extra["layer_violations_stripped"] == 2
    assert Path(r.extra["layer_violations_board"]).read_text() == better
    assert r.extra["layer_warning"].startswith("LAYER RESTRICTION BROKEN")
    assert "RF_ANT" in r.extra["layer_warning"] and "4.0 mm on B.Cu" in r.extra["layer_warning"]


def test_a_single_route_is_held_to_its_layers_too(board, tmp_path, monkeypatch):
    _layered_attempts(monkeypatch, [_routed(_seg("RF_ANT", "B.Cu"))] * 2, [0, 0])
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(krt_route, "_fill", lambda py, pcb: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(autoroute, "_measure_board", lambda py, pcb: {"unconnected": 1, "segments": 0})
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F)
    # The route that "finished" with nothing open had its only RF track on B.Cu.
    assert r.unrouted == 1 and r.extra["layer_violations"][0]["net"] == "RF_ANT"
    assert "(segment" not in board.read_text()


def test_the_review_carries_the_layer_warning():
    from blpl.core import stage8_review

    md = stage8_review._render_markdown({
        "summary": {"emitter": 0, "design": 0, "expected": 0}, "placeholders": [], "emitter_defects": [],
        "design_issues": [], "expected": [], "expected_issues": [],
        "autoroute": {"attempted": True, "ok": True, "unrouted": 4, "router": "kicad-routing-tools",
                      "layer_warning": "LAYER RESTRICTION BROKEN on every attempt: RF_ANT"}})
    assert "**LAYER RESTRICTION BROKEN on every attempt: RF_ANT.**" in md[md.index("## Routing"):]


def test_restricted_nets_are_protected_from_krts_rip_up_while_it_routes(board, tmp_path, monkeypatch):
    """KRT's plane finalize ripped RF_LRHF, excluded from that pass with "!",
    and rerouted it on B.Cu: that rip honours KRT's protection list only."""
    pro = board.with_suffix(".kicad_pro")
    before = pro.read_text()
    seen = {}

    def steps(pcb, **kw):
        seen.update(json.loads(pro.read_text()))
        return autoroute.RouteResult(True, "", pcb=str(pcb), unrouted=0)

    monkeypatch.setattr(krt_route, "_route_steps", steps)
    krt_route._route_once(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F)
    assert seen["kicad_routing_tools"]["protected_nets"] == {"RF_ANT": krt_route.LAYER_PROTECTION}
    assert seen["net_settings"] == PRO["net_settings"]
    # The project file is BLPL's, and goes back exactly as it was...
    assert pro.read_text() == before
    # ...even when the route raises.
    monkeypatch.setattr(krt_route, "_route_steps", lambda pcb, **kw: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        krt_route._route_once(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F)
    assert pro.read_text() == before
    # Nothing restricted: the file is not touched at all.
    monkeypatch.setattr(krt_route, "_route_steps", steps)
    seen.clear()
    krt_route._route_once(board, work_dir=tmp_path / "w", class_layers=None)
    assert "kicad_routing_tools" not in seen


def test_each_setting_is_routed_protected_then_not_and_the_best_clean_one_kept(board, tmp_path, monkeypatch):
    """Protection keeps KRT's plane finalize off RF, but also every rip-up that
    would have moved RF for room: ear-tag-bm20 left 3 open protected and 1
    not, with RF on F.Cu either way. Both are tried; the guard ranks them."""
    clean = _routed(_seg("RF_ANT", "F.Cu"))
    stray = _routed(_seg("RF_ANT", "B.Cu"))
    once = _layered_attempts(monkeypatch, [clean, clean, stray, clean], [3, 1, 0, 2])
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F,
                        retry=[{"direction": "backward"}])
    assert once.protects == [True, False, True, False]
    assert [(a["search"], a["protected"]) for a in r.extra["attempts"]] == [
        ({}, True), ({}, False), ({"direction": "backward"}, True), ({"direction": "backward"}, False)]
    # The unprotected default left fewest open and kept RF on its layer.
    assert r.extra["kept_attempt"] == 1 and r.unrouted == 1 and "layer_warning" not in r.extra


def test_a_clean_protected_route_skips_its_unprotected_twin(board, tmp_path, monkeypatch):
    once = _layered_attempts(monkeypatch, [_routed(_seg("RF_ANT", "F.Cu"))], [0])
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F)
    assert once.protects == [True] and r.unrouted == 0 and r.extra["kept_attempt"] == 0


def test_with_nothing_restricted_each_setting_is_routed_once(board, tmp_path, monkeypatch):
    once = _layered_attempts(monkeypatch, [_routed(_seg("RF_ANT", "B.Cu"))] * 2, [2, 1])
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=None, retry=[{"direction": "backward"}])
    assert once.protects == [True, True]
    assert all("protected" not in a for a in r.extra["attempts"])


def test_an_unprotected_route_leaves_the_project_file_alone(board, tmp_path, monkeypatch):
    pro = board.with_suffix(".kicad_pro")
    seen = []
    monkeypatch.setattr(krt_route, "_route_steps",
                        lambda pcb, **kw: seen.append(json.loads(pro.read_text()))
                        or autoroute.RouteResult(True, "", pcb=str(pcb), unrouted=0))
    krt_route._route_once(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F, protect=False)
    assert "kicad_routing_tools" not in seen[0]


def _narrow(net: str, layer: str = "F.Cu", width: float = 0.2) -> str:
    return f'(segment (start 0 0) (end 4 0) (width {width}) (layer "{layer}") (net "{net}"))'


def test_a_restricted_net_on_its_layer_at_another_width_is_a_stray_too(board):
    """Unprotected, KRT's finalize repairs RF with the last pass's layers and
    width: on F.Cu at the Default 0.2 mm it passes a layer check, and is not
    50 ohm (Codex, PR #109)."""
    board.write_text(_routed(_seg("RF_ANT", "F.Cu"), _narrow("RF_ANT"), _narrow("SPI_SCK")))
    assert krt_route.layer_violations(board, RF_ONLY_F) == [
        {"net": "RF_ANT", "class": "RF_50R", "allowed": ["F.Cu"], "tracks_mm": {"F.Cu at 0.2 mm": 4.0}, "vias": 0}]


def test_a_narrow_unprotected_route_loses_to_a_correct_one_with_more_open(board, tmp_path, monkeypatch):
    right = _routed(_seg("RF_ANT", "F.Cu"))
    narrow = _routed(_narrow("RF_ANT"))
    once = _layered_attempts(monkeypatch, [right, narrow], [1, 0])
    r = krt_route.route(board, work_dir=tmp_path / "w", class_layers=RF_ONLY_F)
    assert once.protects == [True, False]
    assert r.extra["kept_attempt"] == 0 and r.unrouted == 1 and board.read_text() == right
    assert r.extra["attempts"][1]["layer_violations"] == ["RF_ANT"]
