"""Controlled impedance: which nets need it, on which class, and whether the
board says how to build it.

Before this, impedance lived only in net-class names. `RF_50Ohm` was a string
Stage 4 guessed from a net name, its width was whatever was typed, a pair's gap
was the clearance, and nothing told the fab which traces to control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from blpl.core import impedance as z
from blpl.core import release, schema, skills_install, stage5_emit_yaml_hdm as s5
from blpl.emitter import pro


def _config(**extra) -> dict:
    cfg = {
        "project": {"name": "p", "dimensions": [50, 40]},
        "net_classes": {"Default": {"trace_width": 0.2, "clearance": 0.15,
                                    "via_dia": 0.6, "via_drill": 0.3}},
        "test_points": {"policy": "none"},
    }
    for k, v in extra.items():
        if k == "classes":
            cfg["net_classes"].update(v)
        else:
            cfg[k] = v
    return cfg


def _nets(*names: str, pairs: dict[str, str] | None = None, cls: str = "Default") -> dict:
    out = {n: {"class": cls, "pads": [["U1", str(i)]]} for i, n in enumerate(names, 1)}
    for a, b in (pairs or {}).items():
        out[a]["diff_pair_of"] = b
        out[b]["diff_pair_of"] = a
    return out


# --- parsing --------------------------------------------------------------------------


@pytest.mark.parametrize("raw, ohms, mode", [
    (True, 50, "single_ended"),
    (90, 90, "single_ended"),
    ({"ohms": 100, "mode": "differential"}, 100, "differential"),
    ({}, 50, "single_ended"),
])
def test_an_impedance_can_be_written_four_ways_and_defaults_to_50_single_ended(raw, ohms, mode):
    spec = z.parse(raw, "x")
    assert (spec.ohms, spec.mode, spec.tolerance_pct) == (ohms, mode, 10)


@pytest.mark.parametrize("raw, says", [
    (5, "from 10 to 200"),
    ({"ohms": 50, "mode": "diff"}, "mode must be"),
    ({"tolerance_pct": 0}, "tolerance_pct"),
    ("fifty", "true, a number"),
])
def test_a_typo_in_an_impedance_is_refused_by_name(raw, says):
    with pytest.raises(z.ImpedanceConfigError, match=says):
        z.parse(raw, "net_classes.RF.impedance")


@pytest.mark.parametrize("tag, classes, says", [
    ({"nets": "RF", "class": "Nope"}, {}, "not in net_classes"),
    ({"nets": "RF", "class": "RF"}, {"RF": {"trace_width": 0.3}}, "declares no impedance"),
    ({"nets": "RF("}, {}, "not a regex"),
    ({"nets": "RF", "class": "RF", "ohms": 75}, {"RF": {"trace_width": 0.3, "impedance": 50}}, "class 'RF' is"),
    ({"ohms": 50}, {}, "needs 'nets'"),
])
def test_a_tag_that_cannot_be_honoured_is_refused(tag, classes, says):
    with pytest.raises(z.ImpedanceConfigError, match=says):
        z.tags(_config(classes=classes, controlled_impedance=[tag]))


# --- resolving ------------------------------------------------------------------------


def test_a_bare_tag_is_50_ohm_single_ended_on_the_matching_declared_class():
    cfg = _config(classes={"RF_50Ohm": {"trace_width": 0.35, "impedance": 50}},
                  controlled_impedance=[{"nets": "_RF_IN$"}])
    nets = _nets("GNSS_RF_IN", "UART_TX")
    res = z.resolve(cfg, nets)
    assert nets["GNSS_RF_IN"]["class"] == "RF_50Ohm"
    assert nets["GNSS_RF_IN"]["impedance"] == {"ohms": 50, "mode": "single_ended", "tolerance_pct": 10}
    assert "impedance" not in nets["UART_TX"]
    assert res.synthesized == {}


def test_two_classes_with_the_same_target_make_a_tag_say_which():
    cfg = _config(classes={"A": {"trace_width": 0.3, "impedance": 50},
                           "B": {"trace_width": 0.1, "impedance": 50}},
                  controlled_impedance=[{"nets": "RF"}])
    with pytest.raises(z.ImpedanceConfigError, match="A, B all meet it"):
        z.resolve(cfg, _nets("RF"))


def test_with_no_class_for_it_one_is_made_with_the_default_geometry():
    cfg = _config(controlled_impedance=[{"nets": "^MIPI_", "ohms": 100, "mode": "differential"}])
    nets = _nets("MIPI_D0_P", "MIPI_D0_N", pairs={"MIPI_D0_P": "MIPI_D0_N"})
    res = z.resolve(cfg, nets)
    assert set(res.synthesized) == {"Z100_DIFF"}
    assert nets["MIPI_D0_P"]["class"] == nets["MIPI_D0_N"]["class"] == "Z100_DIFF"
    made = z.synthesized_classes(cfg, res)["Z100_DIFF"]
    assert made["trace_width"] == 0.2 and made["synthesized"] == z.SYNTHESIZED


def test_a_differential_tag_on_one_half_brings_the_other():
    cfg = _config(controlled_impedance=[{"nets": "_DP$", "ohms": 90, "mode": "differential"}])
    nets = _nets("USB_DP", "USB_DM", pairs={"USB_DP": "USB_DM"})
    res = z.resolve(cfg, nets)
    assert nets["USB_DM"]["impedance"]["ohms"] == 90
    assert any("USB_DM" in n for n in res.notes)


def test_an_explicit_tag_beats_the_name_guess():
    cfg = _config(classes={"RF_50Ohm": {"trace_width": 0.35, "impedance": 50},
                           "Z75": {"trace_width": 0.2, "impedance": 75}},
                  controlled_impedance=[{"nets": "VIDEO_RF_OUT", "ohms": 75}])
    nets = _nets("VIDEO_RF_OUT", cls="RF_50Ohm")
    z.resolve(cfg, nets)
    assert nets["VIDEO_RF_OUT"]["class"] == "Z75"


def test_a_net_stage4_put_on_an_impedance_class_is_controlled_without_a_tag():
    cfg = _config(classes={"RF_50Ohm": {"trace_width": 0.35, "impedance": 50}})
    nets = _nets("GNSS_RF_IN", cls="RF_50Ohm")
    z.resolve(cfg, nets)
    assert nets["GNSS_RF_IN"]["impedance"]["ohms"] == 50


def test_one_net_cannot_be_two_impedances():
    cfg = _config(controlled_impedance=[{"nets": "RF"}, {"nets": "RF_IN", "ohms": 75}])
    with pytest.raises(z.ImpedanceConfigError, match="tagged 50"):
        z.resolve(cfg, _nets("RF_IN"))


def test_a_tighter_tolerance_does_not_land_on_a_looser_class():
    """Codex, PR #31: the class's tolerance is what the fab is told, so a ±5%
    net on a ±10% class would be built to ±10%."""
    cfg = _config(classes={"RF_50Ohm": {"trace_width": 0.35, "impedance": 50}},
                  controlled_impedance=[{"nets": "ANT", "tolerance_pct": 5}])
    nets = _nets("ANT")
    res = z.resolve(cfg, nets)
    assert nets["ANT"]["class"] == "Z50_SE_T5"
    assert nets["ANT"]["impedance"]["tolerance_pct"] == 5
    hdm = {"net_classes": {**cfg["net_classes"], **z.synthesized_classes(cfg, res)}, "nets": nets}
    assert "| Z50_SE_T5 | 50 Ω single-ended | ±5% | any |" in z.fab_table(hdm)


def test_a_looser_tolerance_is_met_by_a_tighter_class_and_records_the_class():
    cfg = _config(classes={"RF5": {"trace_width": 0.35, "impedance": {"ohms": 50, "tolerance_pct": 5}}},
                  controlled_impedance=[{"nets": "ANT", "tolerance_pct": 10}])
    nets = _nets("ANT")
    z.resolve(cfg, nets)
    assert nets["ANT"]["class"] == "RF5"
    assert nets["ANT"]["impedance"]["tolerance_pct"] == 5


def test_a_tag_naming_a_looser_class_is_refused():
    cfg = _config(classes={"RF": {"trace_width": 0.35, "impedance": 50}},
                  controlled_impedance=[{"nets": "ANT", "class": "RF", "tolerance_pct": 5}])
    with pytest.raises(z.ImpedanceConfigError, match="class 'RF' is 50"):
        z.tags(cfg)


def test_a_made_class_never_takes_the_name_of_a_declared_one():
    """Codex, PR #31: a declared Z50_SE would have been overwritten with
    Default's width, and every net on it marked as controlled."""
    cfg = _config(classes={"Z50_SE": {"trace_width": 0.6}},
                  controlled_impedance=[{"nets": "ANT"}])
    nets = {**_nets("ANT"), "VBUS": {"class": "Z50_SE", "pads": [["U1", "9"]]}}
    res = z.resolve(cfg, nets)
    assert set(res.synthesized) == {"Z50_SE_2"}
    assert nets["ANT"]["class"] == "Z50_SE_2"
    assert nets["VBUS"]["class"] == "Z50_SE" and "impedance" not in nets["VBUS"]


def test_a_tag_that_matches_nothing_says_so():
    res = z.resolve(_config(controlled_impedance=[{"nets": "^NOPE"}]), _nets("RF_IN"))
    assert res.notes and "matches no net" in res.notes[0]


# --- Stage 5, end to end --------------------------------------------------------------


def _stage5_inputs(signals: list[tuple[str, str]]):
    da = {"project_id": "proj", "schema_version": 1, "source_files": [], "components": [],
          "connectors": [{"local_id": "J1", "pins": [{"pin": p, "signal": s} for p, s in signals]}],
          "subsystems": [], "raw_nets": []}
    bom = {"project_id": "proj", "schema_version": 1, "rows": [
        {"local_id": "J1", "refdes": "J1", "mpn": "X", "package": "Header",
         "footprint_hint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
         "symbol_hint": "Connector:Conn_01x04", "confidence": 0.9}]}
    nets = {"project_id": "proj", "schema_version": 1, "nets": [
        {"name": s, "class": "Default", "members": [{"refdes": "J1", "pin": p}]} for p, s in signals]}
    return bom, nets, da


def test_stage5_puts_the_tag_on_the_net_and_the_record_says_so():
    cfg = _config(controlled_impedance=[{"nets": "ANT"}])
    cfg["test_points"] = {"policy": "all"}
    hdm, _, _ = s5.emit(*_stage5_inputs([("1", "ANT_FEED"), ("2", "GPIO1")]), cfg)
    assert hdm["nets"]["ANT_FEED"]["class"] == "Z50_SE"
    assert hdm["nets"]["ANT_FEED"]["impedance"]["ohms"] == 50
    assert hdm["net_classes"]["Z50_SE"]["synthesized"] == z.SYNTHESIZED
    assert hdm["synthesis"]["controlled_impedance"]["synthesized"] == ["Z50_SE"]
    # A test pad on the antenna feed is a stub; the GPIO still gets one.
    tp_nets = {c.get("net") for c in hdm["components"].values() if c.get("synthesized")}
    assert tp_nets == {"GPIO1"}


def test_stage5_refuses_a_file_whose_tag_names_a_class_that_is_not_there(tmp_path):
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(_config(controlled_impedance=[{"nets": "X", "class": "Ghost"}])))
    with pytest.raises(s5.InvalidProjectConfigError, match="Ghost"):
        s5.ensure_project_config(path, "proj")


def test_the_schema_accepts_the_documented_forms():
    cfg = _config(
        classes={"RF": {"trace_width": 0.35, "impedance": 50},
                 "USB": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                         "impedance": {"ohms": 90, "mode": "differential"}}},
        controlled_impedance=[{"nets": "RF"}, {"nets": "USB", "ohms": 90, "mode": "differential"}],
    )
    assert [e.message for e in schema.validator("project_config").iter_errors(cfg)] == []


# --- .kicad_pro -----------------------------------------------------------------------


def test_a_pairs_gap_is_its_own_number_not_the_clearance():
    """The gap sets a differential impedance as much as the width; it used to
    be written as the clearance."""
    hdm = {"net_classes": {"USB": {"trace_width": 0.18, "clearance": 0.2, "diff_pair_gap": 0.15,
                                   "diff_pair_width": 0.17}}, "nets": {}}
    (usb,) = [c for c in pro.build(hdm, project_filename="p.kicad_pro")["net_settings"]["classes"]
              if c["name"] == "USB"]
    assert (usb["diff_pair_gap"], usb["diff_pair_width"], usb["clearance"]) == (0.15, 0.17, 0.2)


# --- Stage 8 --------------------------------------------------------------------------


def _ids(findings):
    return sorted(f["rule_id"] for f in findings)


def test_a_made_up_class_is_an_error_until_a_real_width_is_declared():
    hdm = {"net_classes": {"Z50_SE": {"trace_width": 0.2, "impedance": {"ohms": 50},
                                      "synthesized": z.SYNTHESIZED}},
           "nets": {"ANT": {"class": "Z50_SE", "impedance": {"ohms": 50}}}}
    (f,) = z.review(hdm)
    assert (f["rule_id"], f["severity"], f["nets"]) == ("ZC-001", "error", ["ANT"])


def test_a_differential_class_needs_a_gap_and_its_nets_need_partners():
    hdm = {"net_classes": {"USB": {"trace_width": 0.18, "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}}}}
    assert _ids(z.review(hdm)) == ["ZC-002", "ZC-003"]


def test_a_class_named_for_an_impedance_that_declares_none_is_flagged():
    hdm = {"net_classes": {"RF_50Ohm": {"trace_width": 0.3}},
           "nets": {"GNSS_RF_IN": {"class": "RF_50Ohm"}}}
    (f,) = z.review(hdm)
    assert f["rule_id"] == "ZC-004" and "impedance: 50" in f["recommendation"]


def test_a_finished_impedance_class_is_quiet():
    hdm = {"net_classes": {"USB": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                   "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DM"},
                    "USB_DM": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DP"}}}
    assert z.review(hdm) == []


# --- the fab sheet and the release ----------------------------------------------------


def test_the_fab_sheet_lists_each_controlled_class_and_says_when_a_width_is_missing():
    hdm = {"net_classes": {"RF": {"trace_width": 0.35, "impedance": 50},
                           "Z90_DIFF": {"trace_width": 0.2, "impedance": {"ohms": 90, "mode": "differential"},
                                        "synthesized": z.SYNTHESIZED}},
           "nets": {"ANT": {"class": "RF", "impedance": {"ohms": 50}},
                    "USB_DP": {"class": "Z90_DIFF", "impedance": {"ohms": 90}}}}
    table = z.fab_table(hdm)
    assert "| RF | 50 Ω single-ended | ±10% | any | 0.35 | — | ANT |" in table
    assert "| Z90_DIFF | 90 Ω differential | ±10% | any | NOT SET | NOT SET | USB_DP |" in table
    assert z.fab_table({"net_classes": {}, "nets": {"X": {"class": "Default"}}}) is None


@pytest.fixture
def released(tmp_path: Path, monkeypatch):
    proj = tmp_path / "dev"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "dev.kicad_pcb").write_text("(kicad_pcb)\n")
    (proj / ".pipeline" / "bom_resolved.json").write_text(json.dumps([]))
    monkeypatch.setattr(release, "find_kicad_cli", lambda: None)
    monkeypatch.setattr(release, "run_gate", lambda *_a, **_k: {"ok": True})

    def build(hdm):
        (proj / ".pipeline" / "hdm.yaml").write_text(yaml.safe_dump(hdm))
        return release.build(proj, now="2026-10-15_120000Z")
    return proj, build


def test_a_release_carries_the_fab_sheet(released):
    proj, build = released
    manifest = build({"net_classes": {"RF": {"trace_width": 0.35, "impedance": 50}},
                      "nets": {"ANT": {"class": "RF", "impedance": {"ohms": 50}}}})
    assert manifest["quotable"]
    assert "fab/impedance.md" in [f["path"] for f in manifest["files"]]


def test_a_release_with_an_unset_impedance_width_is_not_quotable_and_says_why(released):
    """The gate passed; the board still cannot be built to its impedances."""
    proj, build = released
    manifest = build({"net_classes": {"Z50_SE": {"trace_width": 0.2, "impedance": 50,
                                                 "synthesized": z.SYNTHESIZED}},
                      "nets": {"ANT": {"class": "Z50_SE", "impedance": {"ohms": 50}}}})
    assert not manifest["quotable"]
    assert "impedance" in manifest["incomplete"]
    readme = next(p for p in (proj / "release").rglob("READ-ME-FIRST.txt"))
    text = readme.read_text()
    assert "gate passed, but" in text and "impedance" in text


# --- the skill ------------------------------------------------------------------------


def test_the_skill_ships_and_installs_by_default():
    assert "controlled-impedance" in skills_install.DEFAULT_SET
    assert "controlled-impedance" in skills_install.available(source=Path("/nonexistent"))


# --- pairs, both halves (Codex, PR #31) -----------------------------------------------


def test_a_pair_half_classed_by_a_rule_brings_its_partner_onto_the_class():
    """A net_class_rules pattern matched _DP and not _DM: the minus half stayed
    on Default, routed with other geometry and missing from the fab sheet."""
    cfg = _config(classes={"USB": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                   "impedance": {"ohms": 90, "mode": "differential"}}})
    nets = _nets("USB_DP", "USB_DM", pairs={"USB_DP": "USB_DM"})
    nets["USB_DP"]["class"] = "USB"
    res = z.resolve(cfg, nets)
    assert nets["USB_DM"]["class"] == "USB" and nets["USB_DM"]["impedance"]["ohms"] == 90
    assert any("USB_DM" in n for n in res.notes)


def test_a_pair_split_across_two_classes_is_an_error():
    hdm = {"net_classes": {"A": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                 "impedance": {"ohms": 90, "mode": "differential"}},
                           "B": {"trace_width": 0.2, "diff_pair_gap": 0.1,
                                 "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"X_P": {"class": "A", "impedance": {"ohms": 90}, "diff_pair_of": "X_N"},
                    "X_N": {"class": "B", "impedance": {"ohms": 90}, "diff_pair_of": "X_P"}}}
    found = z.review(hdm)
    assert [f["rule_id"] for f in found] == ["ZC-005", "ZC-005"]
    assert all(f["severity"] == "error" for f in found)


def test_the_fab_sheet_gives_the_width_a_pair_is_routed_at():
    """.kicad_pro routes a pair at diff_pair_width; the sheet said trace_width."""
    hdm = {"net_classes": {"USB": {"trace_width": 0.25, "diff_pair_width": 0.18,
                                   "diff_pair_gap": 0.15,
                                   "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DM"},
                    "USB_DM": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DP"}}}
    assert "| USB | 90 Ω differential | ±10% | any | 0.18 | 0.15 |" in z.fab_table(hdm)


def test_a_pair_width_alone_is_a_stackup_width_for_a_differential_class():
    hdm = {"net_classes": {"USB": {"diff_pair_width": 0.18, "diff_pair_gap": 0.15,
                                   "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DM"},
                    "USB_DM": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DP"}}}
    assert z.review(hdm) == []


# --- coplanar and release errors (Codex, PR #31) --------------------------------------


def test_a_coplanar_class_needs_its_ground_gap_and_the_sheet_prints_it():
    hdm = {"net_classes": {"CPW": {"trace_width": 0.3, "impedance": {"ohms": 50, "mode": "coplanar"}}},
           "nets": {"ANT": {"class": "CPW", "impedance": {"ohms": 50}}}}
    (f,) = z.review(hdm)
    assert (f["rule_id"], f["severity"]) == ("ZC-006", "error")
    assert "| CPW | 50 Ω coplanar | ±10% | any | 0.3 | NOT SET | ANT |" in z.fab_table(hdm)
    hdm["net_classes"]["CPW"]["coplanar_gap"] = 0.15
    assert z.review(hdm) == []
    assert "| CPW | 50 Ω coplanar | ±10% | any | 0.3 | 0.15 | ANT |" in z.fab_table(hdm)


def test_a_coplanar_gap_is_the_clearance_the_pour_keeps():
    hdm = {"net_classes": {"CPW": {"trace_width": 0.3, "clearance": 0.2, "coplanar_gap": 0.15,
                                   "impedance": {"ohms": 50, "mode": "coplanar"}}}, "nets": {}}
    (cpw,) = [c for c in pro.build(hdm, project_filename="p.kicad_pro")["net_settings"]["classes"]
              if c["name"] == "CPW"]
    assert cpw["clearance"] == 0.15


def test_a_release_with_a_pair_split_across_complete_classes_is_not_quotable(released):
    """Every cell of the sheet is filled, and it still asks for two geometries
    for one coupled pair."""
    proj, build = released
    full = {"trace_width": 0.18, "diff_pair_gap": 0.15, "impedance": {"ohms": 90, "mode": "differential"}}
    manifest = build({"net_classes": {"A": full, "B": {**full, "diff_pair_gap": 0.1}},
                      "nets": {"X_P": {"class": "A", "impedance": {"ohms": 90}, "diff_pair_of": "X_N"},
                               "X_N": {"class": "B", "impedance": {"ohms": 90}, "diff_pair_of": "X_P"}}})
    assert not manifest["quotable"]
    (step,) = [s for s in manifest["steps"] if s["step"] == "impedance"]
    assert "ZC-005" in step["detail"]



# --- layers, unpaired nets, the whole list (Codex, PR #31) ----------------------------


def _four_layer(cls_rules: dict, nets: dict) -> dict:
    return {"project": {"stackup": {"layers": 4}}, "net_classes": {"RF": cls_rules}, "nets": nets}


def test_on_a_multilayer_board_a_class_must_say_which_layer_its_width_is_for():
    rules = {"trace_width": 0.35, "impedance": 50}
    hdm = _four_layer(rules, {"ANT": {"class": "RF", "impedance": {"ohms": 50}}})
    (f,) = z.review(hdm)
    assert (f["rule_id"], f["severity"]) == ("ZC-007", "error")
    rules["impedance"] = {"ohms": 50, "layers": ["F.Cu"]}
    assert z.review(hdm) == []
    assert "| RF | 50 Ω single-ended | ±10% | F.Cu | 0.35 | — | ANT |" in z.fab_table(hdm)


def test_a_two_layer_board_needs_no_layer():
    hdm = {"project": {"stackup": {"layers": 2}},
           "net_classes": {"RF": {"trace_width": 0.35, "impedance": 50}},
           "nets": {"ANT": {"class": "RF", "impedance": {"ohms": 50}}}}
    assert z.review(hdm) == []


def test_an_unpaired_differential_net_is_an_error():
    hdm = {"net_classes": {"USB": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                   "impedance": {"ohms": 90, "mode": "differential"}}},
           "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}}}}
    (f,) = z.review(hdm)
    assert (f["rule_id"], f["severity"]) == ("ZC-003", "error")


def test_the_fab_sheet_names_every_controlled_net():
    """The gerbers do not say which traces are controlled; a name left off is a
    trace the fab will not control."""
    names = [f"N{i:02d}" for i in range(12)]
    hdm = {"net_classes": {"RF": {"trace_width": 0.35, "impedance": 50}},
           "nets": {n: {"class": "RF", "impedance": {"ohms": 50}} for n in names}}
    table = z.fab_table(hdm)
    assert all(n in table for n in names) and "more" not in table


@pytest.mark.parametrize("pcb_text", [
    # KiCad 10: a segment names its net.
    '(kicad_pcb (segment (start 0 0) (end 1 0) (width 0.35) (layer "F.Cu") (net "ANT"))'
    ' (segment (start 1 0) (end 2 0) (width 0.35) (layer "In2.Cu") (net "ANT")))',
    # Before it: an ordinal, resolved through the board's net table.
    '(kicad_pcb (net 0 "") (net 3 "ANT")'
    ' (segment (start 0 0) (end 1 0) (width 0.35) (layer "F.Cu") (net 3))'
    ' (segment (start 1 0) (end 2 0) (width 0.35) (layer "In2.Cu") (net 3)))',
])
def test_a_controlled_trace_routed_off_its_layer_is_found_in_either_net_syntax(pcb_text):
    from blpl.emitter import sexpr

    hdm = _four_layer({"trace_width": 0.35, "impedance": {"ohms": 50, "layers": ["F.Cu"]}},
                      {"ANT": {"class": "RF", "impedance": {"ohms": 50}}})
    assert z.layer_violations(hdm, sexpr.parse(pcb_text)) == [("ANT", "In2.Cu", "RF")]


def test_a_release_with_a_controlled_trace_off_its_layer_is_not_quotable(released):
    proj, build = released
    (proj / "dev.kicad_pcb").write_text(
        '(kicad_pcb (segment (start 0 0) (end 1 0) (width 0.35) (layer "B.Cu") (net "ANT")))\n')
    manifest = build(_four_layer({"trace_width": 0.35, "impedance": {"ohms": 50, "layers": ["F.Cu"]}},
                                 {"ANT": {"class": "RF", "impedance": {"ohms": 50}}}))
    assert not manifest["quotable"]
    (step,) = [s for s in manifest["steps"] if s["step"] == "impedance"]
    assert "ANT on B.Cu" in step["detail"]


# --- routed width, coplanar ground, partial tags (Codex, PR #31) ----------------------


def test_a_controlled_trace_routed_at_another_width_is_found():
    from blpl.emitter import sexpr

    hdm = {"net_classes": {"RF": {"trace_width": 0.35, "impedance": 50}},
           "nets": {"ANT": {"class": "RF", "impedance": {"ohms": 50}}}}
    pcb = sexpr.parse('(kicad_pcb (net 0 "") (net 2 "ANT")'
                      ' (segment (start 0 0) (end 1 0) (width 0.35) (layer "F.Cu") (net 2))'
                      ' (segment (start 1 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 2)))')
    assert z.width_violations(hdm, pcb) == [("ANT", 0.2, 0.35, "RF")]


def _filled(net_form: str, layer: str, *rects) -> str:
    polys = " ".join(
        f'(filled_polygon (layer "{layer}") (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1})))'
        for x0, y0, x1, y1 in rects)
    return f'(zone {net_form} (layers "{layer}") (polygon (pts (xy -5 -5) (xy 15 -5) (xy 15 5) (xy -5 5))) {polys})'


def test_a_coplanar_class_needs_filled_ground_beside_the_trace():
    """Codex, PR #31: any ground zone on the layer used to pass, however far
    from the trace and whether or not it was ever filled."""
    from blpl.emitter import sexpr

    hdm = {"net_classes": {"CPW": {"trace_width": 0.3, "coplanar_gap": 0.15,
                                   "impedance": {"ohms": 50, "mode": "coplanar"}}},
           "nets": {"ANT": {"class": "CPW", "impedance": {"ohms": 50}}}}
    seg = '(segment (start 0 0) (end 10 0) (width 0.3) (layer "F.Cu") (net "ANT"))'
    # Copper from 0.3 mm out (0.15 half-width + 0.15 gap) on both sides.
    beside = [(-1, 0.3, 11, 3), (-1, -3, 11, -0.3)]

    def board(*zones):
        return sexpr.parse("(kicad_pcb " + seg + " " + " ".join(zones) + ")")

    assert z.coplanar_without_ground(hdm, board()) == [("CPW", "F.Cu")]
    # Filled, on the layer, in both net spellings: passes.
    assert z.coplanar_without_ground(hdm, board(_filled('(net "GND")', "F.Cu", *beside))) == []
    assert z.coplanar_without_ground(
        hdm, board(_filled('(net 1) (net_name "GND")', "F.Cu", *beside))) == []
    # Ground far from the trace is not beside it.
    far = _filled('(net "GND")', "F.Cu", (-1, 20, 11, 25))
    assert z.coplanar_without_ground(hdm, board(far)) == [("CPW", "F.Cu")]
    # A zone that was never filled has no copper.
    unfilled = '(zone (net "GND") (layers "F.Cu") (polygon (pts (xy -5 -5) (xy 15 -5) (xy 15 5) (xy -5 5))))'
    assert z.coplanar_without_ground(hdm, board(unfilled)) == [("CPW", "F.Cu")]
    # Ground on one side only is not coplanar.
    assert z.coplanar_without_ground(
        hdm, board(_filled('(net "GND")', "F.Cu", beside[0]))) == [("CPW", "F.Cu")]
    # A rule area is not copper.
    keepout = '(zone (layers "F.Cu") (keepout (tracks not_allowed)))'
    assert z.coplanar_without_ground(hdm, board(keepout)) == [("CPW", "F.Cu")]


def test_a_tag_naming_a_class_takes_what_it_leaves_out_from_that_class():
    cfg = _config(classes={"USB90": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                     "impedance": {"ohms": 90, "mode": "differential",
                                                   "tolerance_pct": 5}}},
                  controlled_impedance=[{"nets": "USB", "class": "USB90", "tolerance_pct": 10}])
    (tag,) = z.tags(cfg)
    assert (tag.spec.ohms, tag.spec.mode, tag.spec.tolerance_pct) == (90, "differential", 10)


def test_a_release_with_a_coplanar_line_and_no_ground_is_not_quotable(released):
    proj, build = released
    (proj / "dev.kicad_pcb").write_text(
        '(kicad_pcb (segment (start 0 0) (end 10 0) (width 0.3) (layer "F.Cu") (net "ANT")))\n')
    manifest = build({"net_classes": {"CPW": {"trace_width": 0.3, "coplanar_gap": 0.15,
                                              "impedance": {"ohms": 50, "mode": "coplanar"}}},
                      "nets": {"ANT": {"class": "CPW", "impedance": {"ohms": 50}}}})
    assert not manifest["quotable"]
    (step,) = [s for s in manifest["steps"] if s["step"] == "impedance"]
    assert "CPW has no ground zone on F.Cu" in step["detail"]


# --- a routed pair is coupled at its geometry (Codex, PR #31) ------------------------


_PAIR_HDM = {"net_classes": {"USB": {"trace_width": 0.18, "diff_pair_gap": 0.15,
                                     "impedance": {"ohms": 90, "mode": "differential"}}},
             "nets": {"USB_DP": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DM"},
                      "USB_DM": {"class": "USB", "impedance": {"ohms": 90}, "diff_pair_of": "USB_DP"}}}


def _seg(net, x1, y1, x2, y2, layer="F.Cu", w=0.18):
    return f'(segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) (layer "{layer}") (net "{net}"))'


def _pair_board(*segs):
    from blpl.emitter import sexpr
    return sexpr.parse("(kicad_pcb " + " ".join(segs) + ")")


def test_a_pair_routed_at_its_gap_is_quiet_breakouts_and_all():
    # Centres 0.33 apart: 0.18 wide each, so 0.15 edge to edge. The short
    # stubs at the start are breakouts to the pads and are not measured.
    board = _pair_board(_seg("USB_DP", 0, 0, 0.5, 0.5), _seg("USB_DP", 0.5, 0.5, 20.5, 0.5),
                        _seg("USB_DM", 0, 1.5, 0.5, 0.83), _seg("USB_DM", 0.5, 0.83, 20.5, 0.83))
    assert z.pair_problems(_PAIR_HDM, board) == []


def test_a_pair_routed_at_another_gap_is_found():
    board = _pair_board(_seg("USB_DP", 0, 0, 20, 0), _seg("USB_DM", 0, 0.43, 20, 0.43))
    problems = z.pair_problems(_PAIR_HDM, board)
    assert any("0.250 mm apart on F.Cu, not 0.15" in p for p in problems)


def test_a_pair_split_across_layers_is_found():
    board = _pair_board(_seg("USB_DP", 0, 0, 20, 0), _seg("USB_DM", 0, 0.33, 20, 0.33, layer="B.Cu"))
    problems = z.pair_problems(_PAIR_HDM, board)
    assert "USB_DM is routed on B.Cu without its pair partner" in problems
    assert "USB_DP is routed on F.Cu without its pair partner" in problems


def test_a_release_with_an_uncoupled_pair_is_not_quotable(released):
    proj, build = released
    (proj / "dev.kicad_pcb").write_text(
        "(kicad_pcb " + _seg("USB_DP", 0, 0, 20, 0) + _seg("USB_DM", 0, 0.43, 20, 0.43) + ")\n")
    manifest = build(_PAIR_HDM)
    assert not manifest["quotable"]
    (step,) = [s for s in manifest["steps"] if s["step"] == "impedance"]
    assert "apart on F.Cu" in step["detail"]



def test_a_pair_bend_routed_as_arcs_is_measured():
    """Codex, PR #31: arcs were left out of the coupling check, so a pair drawn
    in arcs at any separation passed."""
    from blpl.emitter import sexpr

    def arc(net, r):
        # A quarter circle about the origin, radius r.
        import math
        m = r / math.sqrt(2)
        return (f'(arc (start {r} 0) (mid {m:.6f} {m:.6f}) (end 0 {r}) (width 0.18) '
                f'(layer "F.Cu") (net "{net}"))')

    good = sexpr.parse("(kicad_pcb " + arc("USB_DP", 5) + arc("USB_DM", 5.33) + ")")
    assert z.pair_problems(_PAIR_HDM, good) == []
    wide = sexpr.parse("(kicad_pcb " + arc("USB_DP", 5) + arc("USB_DM", 6) + ")")
    assert any("apart on F.Cu" in p for p in z.pair_problems(_PAIR_HDM, wide))
    alone = sexpr.parse("(kicad_pcb " + arc("USB_DP", 5) + arc("USB_DM", 5.33).replace("F.Cu", "B.Cu") + ")")
    assert any("with no USB_DM beside it" in p for p in z.pair_problems(_PAIR_HDM, alone))


def test_a_tag_that_matches_nothing_is_an_error_that_blocks_release(tmp_path):
    """Codex, PR #31: a misspelled tag dropped its requirement silently."""
    cfg = _config(controlled_impedance=[{"nets": "^ANNT_"}])
    hdm, _, _ = s5.emit(*_stage5_inputs([("1", "ANT_FEED")]), cfg)
    assert hdm["synthesis"]["controlled_impedance"]["unmatched"] == ["controlled_impedance[0] ('^ANNT_')"]
    (f,) = z.review(hdm)
    assert (f["rule_id"], f["severity"]) == ("ZC-008", "error")



# --- coupled all the way along, arcs included (Codex, PR #31) -------------------------


def test_a_long_half_beside_only_a_scrap_of_its_partner_is_uncoupled():
    board = _pair_board(_seg("USB_DP", 0, 0, 100, 0), _seg("USB_DM", 99.5, 0.33, 100, 0.33))
    problems = z.pair_problems(_PAIR_HDM, board)
    assert any(p.startswith("USB_DP runs") and "with no USB_DM beside it" in p for p in problems)


def test_arcs_on_one_circle_in_different_quadrants_are_not_a_pair():
    from blpl.emitter import sexpr

    q1 = '(arc (start 5 0) (mid 3.535534 3.535534) (end 0 5) (width 0.18) (layer "F.Cu") (net "USB_DP"))'
    # Radius 5.33, the right gap, but the opposite quadrant.
    q3 = ('(arc (start -5.33 0) (mid -3.768879 -3.768879) (end 0 -5.33) (width 0.18) '
          '(layer "F.Cu") (net "USB_DM"))')
    problems = z.pair_problems(_PAIR_HDM, sexpr.parse(f"(kicad_pcb {q1} {q3})"))
    assert any("with no USB_DM beside it" in p for p in problems)


def test_a_curved_coplanar_feed_needs_ground_too():
    import math

    from blpl.emitter import sexpr

    hdm = {"net_classes": {"CPW": {"trace_width": 0.3, "coplanar_gap": 0.15,
                                   "impedance": {"ohms": 50, "mode": "coplanar"}}},
           "nets": {"ANT": {"class": "CPW", "impedance": {"ohms": 50}}}}
    m = 10 / math.sqrt(2)
    feed = (f'(arc (start 10 0) (mid {m:.6f} {m:.6f}) (end 0 10) (width 0.3) '
            f'(layer "F.Cu") (net "ANT"))')
    assert z.coplanar_without_ground(hdm, sexpr.parse(f"(kicad_pcb {feed})")) == [("CPW", "F.Cu")]

    def disc(r, n=180):
        return " ".join(f"(xy {r * math.cos(2 * math.pi * i / n):.4f} {r * math.sin(2 * math.pi * i / n):.4f})"
                        for i in range(n))
    # Ground inside the bend (a disc to r=9.7) and outside it (a band from
    # r=10.3): the samples just past the gap sit at r~9.65 and r~10.35.
    inner = f'(filled_polygon (layer "F.Cu") (pts {disc(9.7)}))'
    outer_band = []
    for i in range(36):
        a0, a1 = 2 * math.pi * i / 36, 2 * math.pi * (i + 1) / 36
        pts = [(10.3 * math.cos(a0), 10.3 * math.sin(a0)), (14 * math.cos(a0), 14 * math.sin(a0)),
               (14 * math.cos(a1), 14 * math.sin(a1)), (10.3 * math.cos(a1), 10.3 * math.sin(a1))]
        outer_band.append('(filled_polygon (layer "F.Cu") (pts '
                          + " ".join(f"(xy {x:.4f} {y:.4f})" for x, y in pts) + "))")
    zone = '(zone (net "GND") (layers "F.Cu") ' + inner + " " + " ".join(outer_band) + ")"
    assert z.coplanar_without_ground(hdm, sexpr.parse(f"(kicad_pcb {feed} {zone})")) == []
