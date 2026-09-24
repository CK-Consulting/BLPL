"""Stage 5's synthesised copper: no-connect pin lists and test points.

Both exist for the same reason. A generated board used to reach review with
every declared-NC pin reported as a mistake and zero probe points on any rail,
and neither was something the design could fix — the design already said the
pin was NC, and no markdown table is where test points belong. So Stage 5 says
what the design declared open, and adds the probe points a policy asks for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blpl.core import schema, stage5_emit_yaml_hdm as s5

REPO = Path(__file__).resolve().parents[1]
STOCK_SYMBOLS = REPO / "kicad-symbols"
STOCK_FOOTPRINTS = REPO / "kicad-footprints"


def _config(policy: str | None = None, **extra) -> dict:
    cfg = {
        "project": {"name": "TP", "board_id": "TP-1", "dimensions": [50, 40],
                    "stackup": {"layers": 2, "thickness": 1.6, "finish": "HASL"}},
        "net_classes": {"Default": {"trace_width": 0.2, "clearance": 0.15,
                                    "via_dia": 0.6, "via_drill": 0.3}},
        "boundaries": {"board_outline": {"type": "rect", "start": [0, 0], "end": [50, 40],
                                         "layer": "Edge.Cuts", "width": 0.1}},
    }
    if policy is not None or extra:
        cfg["test_points"] = {**({"policy": policy} if policy else {}), **extra}
    return cfg


def _artifact() -> dict:
    a = {
        "project_id": "proj", "schema_version": 1, "source_files": [],
        "components": [], "subsystems": [], "raw_nets": [],
        "connectors": [
            {"local_id": "U1", "pins": [
                {"pin": "1", "signal": "VDD"},
                {"pin": "2", "signal": "GND"},
                {"pin": "3", "signal": "SCL"},
                {"pin": "4", "signal": "NC"},
                {"pin": "5", "signal": "N/C"},
                {"pin": "6", "signal": "NC_U1_6"},
                {"pin": "7", "signal": "Reserved"},
                {"pin": "10", "signal": "nc"},
                {"pin": "8", "signal": "SDA"},
            ]},
            {"local_id": "J1", "pins": [
                {"pin": "1", "signal": "VDD"}, {"pin": "2", "signal": "GND"},
            ]},
        ],
    }
    schema.validate("design_artifact", a)
    return a


def _bom(extra_rows: list[dict] | None = None) -> dict:
    b = {"project_id": "proj", "schema_version": 1, "rows": [
        {"local_id": "U1", "refdes": "U1", "mpn": "IC", "package": "SOIC-10", "confidence": 0.9},
        {"local_id": "J1", "refdes": "J1", "mpn": "HDR", "package": "Header", "confidence": 0.9},
        *(extra_rows or []),
    ]}
    schema.validate("bom", b)
    return b


def _nets() -> dict:
    n = {"project_id": "proj", "schema_version": 1, "nets": [
        {"name": "VDD", "class": "Power_Bulk",
         "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "J1", "pin": "1"}]},
        {"name": "GND", "class": "Power_Bulk",
         "members": [{"refdes": "U1", "pin": "2"}, {"refdes": "J1", "pin": "2"}]},
        {"name": "SCL", "class": "Default", "members": [{"refdes": "U1", "pin": "3"}]},
        {"name": "SDA", "class": "Default", "members": [{"refdes": "U1", "pin": "8"}]},
        {"name": "EMPTY", "class": "Default", "members": []},
    ]}
    schema.validate("nets", n)
    return n


# -- no-connect pins ----------------------------------------------------------


def test_declared_nc_pins_are_listed_by_physical_number() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(), _config("none"))
    # Every NC spelling Stage 4 drops, the NC_<part>_<n> form, and "Reserved";
    # numeric-aware order so pin 10 sorts after pin 7.
    assert hdm["components"]["U1"]["no_connect_pins"] == ["4", "5", "6", "7", "10"]
    # A part with nothing declared NC does not carry the key at all.
    assert "no_connect_pins" not in hdm["components"]["J1"]


def test_a_nc_named_pin_that_some_net_claims_is_not_marked() -> None:
    """A no-connect marker on top of a net label is an ERC error we would have
    written ourselves. The net wins; the marker is withheld."""
    nets = _nets()
    nets["nets"].append({"name": "NC_U1_6", "class": "Default",
                         "members": [{"refdes": "U1", "pin": "6"}]})
    hdm, _, _ = s5.emit(_bom(), nets, _artifact(), _config("none"))
    assert "6" not in hdm["components"]["U1"]["no_connect_pins"]


@pytest.mark.parametrize("signal,expected", [
    ("NC", True), ("n/c", True), ("N.C.", True), ("-", True), ("", True), ("DNC", True),
    ("NC_CELL_31", True), ("Reserved", True), ("RESERVED", True),
    ("NCS", False), ("SYNC", False), ("VCC", False), ("ENC_A", False),
])
def test_the_nc_predicate_matches_stage4_plus_the_documented_spellings(signal, expected) -> None:
    assert s5.is_no_connect(signal) is expected


# -- test points --------------------------------------------------------------


def test_default_policy_is_power_and_the_record_says_so() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(), _config())
    rec = hdm["synthesis"]["test_points"]
    assert rec == {"policy": "power", "count": 2,
                   "symbol": "Connector:TestPoint",
                   "footprint": "TestPoint:TestPoint_Pad_D1.0mm"}
    tps = {r: c for r, c in hdm["components"].items() if c.get("synthesized") == "test_point"}
    assert set(tps) == {"TP1", "TP2"}
    # Sorted by net name: GND before VDD.
    assert tps["TP1"]["net"] == "GND" and tps["TP1"]["value"] == "TP_GND"
    assert tps["TP2"]["net"] == "VDD"
    assert tps["TP1"]["pin_map"] == {"1": "1"}
    assert ["TP1", "1"] in hdm["nets"]["GND"]["pads"]
    assert ["TP2", "1"] in hdm["nets"]["VDD"]["pads"]
    # Signal nets untouched under `power`.
    assert all(p[0] != "TP" for p in hdm["nets"]["SCL"]["pads"])


def test_policy_none_adds_nothing_but_still_records() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(), _config("none"))
    assert not any(c.get("synthesized") for c in hdm["components"].values())
    assert hdm["synthesis"]["test_points"]["policy"] == "none"
    assert hdm["synthesis"]["test_points"]["count"] == 0


def test_policy_all_covers_every_net_that_has_a_pad() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(), _config("all"))
    covered = {c["net"] for c in hdm["components"].values() if c.get("synthesized")}
    # EMPTY has no pads — nothing to probe, so no test point.
    assert covered == {"VDD", "GND", "SCL", "SDA"}
    assert hdm["synthesis"]["test_points"]["count"] == 4


def test_a_hand_placed_tp_keeps_its_refdes() -> None:
    bom = _bom([{"local_id": "TP1", "refdes": "TP1", "mpn": "5019", "package": "TP",
                 "confidence": 0.9}])
    hdm, _, _ = s5.emit(bom, _nets(), _artifact(), _config("power"))
    assert hdm["components"]["TP1"]["value"] == "5019"
    assert "synthesized" not in hdm["components"]["TP1"]
    synth = sorted(r for r, c in hdm["components"].items() if c.get("synthesized"))
    assert synth == ["TP2", "TP3"]


def test_symbol_and_footprint_overrides_are_honoured() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(),
                        _config("power", symbol="My:TP", footprint="My:TP_Pad"))
    tp = hdm["components"]["TP1"]
    assert (tp["lib_symbol"], tp["footprint"]) == ("My:TP", "My:TP_Pad")
    assert hdm["synthesis"]["test_points"]["symbol"] == "My:TP"


def test_an_unknown_policy_is_refused_loudly() -> None:
    with pytest.raises(s5.TestPointConfigError):
        s5.emit(_bom(), _nets(), _artifact(), _config("some"))


@pytest.mark.skipif(not (STOCK_SYMBOLS.is_dir() and STOCK_FOOTPRINTS.is_dir()),
                    reason="stock KiCad libraries not checked out")
def test_the_defaults_resolve_against_the_stock_libraries(tmp_path: Path) -> None:
    """The defaults must be real library parts — a synthesised component that
    resolves to nothing would be a dangling lib_id of our own making."""
    hdm, _, _ = s5.emit(_bom(), _nets(), _artifact(), _config("power"),
                        project_dir=tmp_path, stock_symbols_root=STOCK_SYMBOLS,
                        stock_footprints_root=STOCK_FOOTPRINTS)
    assert hdm["components"]["TP1"]["lib_symbol"] == "Connector:TestPoint"
    assert hdm["components"]["TP1"]["footprint"] == "TestPoint:TestPoint_Pad_D1.0mm"


@pytest.mark.skipif(not (STOCK_SYMBOLS.is_dir() and STOCK_FOOTPRINTS.is_dir()),
                    reason="stock KiCad libraries not checked out")
def test_a_test_point_part_that_does_not_exist_stops_the_stage(tmp_path: Path) -> None:
    with pytest.raises(s5.TestPointConfigError, match="does not exist"):
        s5.emit(_bom(), _nets(), _artifact(), _config("power", symbol="Nope:Missing"),
                project_dir=tmp_path, stock_symbols_root=STOCK_SYMBOLS,
                stock_footprints_root=STOCK_FOOTPRINTS)


def test_a_project_rule_beats_stage4s_guess(tmp_path) -> None:
    """Stage 4's name rules must work for every board, so they cannot know this
    project spells its USB class USB2_HS rather than USB2_Diff_90Ohm. Without a
    project rule the class it declares is used by nothing."""
    from blpl.core import stage5_emit_yaml_hdm as s5

    cfg = {"net_classes": {"Default": {}, "USB2_HS": {}},
           "net_class_rules": [{"pattern": r"^MCU_USB_D[PMN]$", "class": "USB2_HS"}]}
    rules = s5._project_class_rules(cfg)
    declared = set(cfg["net_classes"])
    assert s5._reconcile_net_class("MCU_USB_DP", "USB2_Diff_90Ohm", rules, declared) == ("USB2_HS", None)
    assert s5._reconcile_net_class("MCU_USB_DM", "Default", rules, declared) == ("USB2_HS", None)


def test_an_undeclared_class_is_reported_but_not_rewritten(tmp_path) -> None:
    """Demoting the net here was tried and is worse: a project that merely
    forgot to declare Power_Bulk would have its rails silently rewritten to a
    signal trace width by the pipeline. Emit what was asked for, and say so."""
    from blpl.core import stage5_emit_yaml_hdm as s5

    cls, note = s5._reconcile_net_class("VBAT", "Power_Bulk", [], {"Default"})
    assert cls == "Power_Bulk"
    assert "does not declare" in note


def test_no_declared_classes_means_no_opinion(tmp_path) -> None:
    """An absent net_classes block is 'no opinion', not 'nothing permitted'."""
    from blpl.core import stage5_emit_yaml_hdm as s5

    assert s5._reconcile_net_class("VBAT", "Power_Bulk", [], set()) == ("Power_Bulk", None)


def test_a_rule_naming_an_undeclared_class_is_refused(tmp_path) -> None:
    from blpl.core import stage5_emit_yaml_hdm as s5

    import pytest as _pytest
    with _pytest.raises(s5.NetClassConfigError, match="not in net_classes"):
        s5._project_class_rules({"net_classes": {"Default": {}},
                                 "net_class_rules": [{"pattern": "^X", "class": "Nope"}]})
