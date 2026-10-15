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
    with pytest.raises(z.ImpedanceConfigError, match="A, B all declare it"):
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
    assert "| RF | 50 Ω single-ended | ±10% | 0.35 | — | ANT |" in table
    assert "| Z90_DIFF | 90 Ω differential | ±10% | NOT SET | NOT SET | USB_DP |" in table
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
