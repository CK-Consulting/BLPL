"""Stage 6a with KiCad Routing Tools (blpl.core.krt_route)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
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
            set_net(node, "GND" if sexpr.unquote(sexpr.find(node, "layer")[1]) == "In1.Cu" else "VDD")
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
    monkeypatch.setattr(krt_route, "_run", lambda cmd, t: next(runs, ok))
    result = krt_route.route(board, work_dir=tmp_path / "w", class_layers={"RF_50R": ["F.Cu"]})
    assert not result.ok and calls == [("bands", True), ("bands", False)]
