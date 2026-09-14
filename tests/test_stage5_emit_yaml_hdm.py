"""Unit tests for pipeline.stage5_emit_yaml_hdm."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from blpl.core import schema, stage5_emit_yaml_hdm as s5


def _project_config() -> dict:
    return {
        "project": {
            "name": "TestProj",
            "board_id": "TP-1",
            "dimensions": [100, 80],
            "stackup": {"layers": 4, "thickness": 1.6, "finish": "ENIG"},
        },
        "net_classes": {
            "Default": {"trace_width": 0.2, "clearance": 0.15, "via_dia": 0.6, "via_drill": 0.3},
            "Power_Bulk": {"trace_width": 0.6, "clearance": 0.2, "via_dia": 0.8, "via_drill": 0.4},
        },
        "boundaries": {
            "board_outline": {
                "type": "rect",
                "start": [0, 0],
                "end": [100, 80],
                "layer": "Edge.Cuts",
                "width": 0.1,
            }
        },
    }


def _design_artifact() -> dict:
    a = {
        "project_id": "proj",
        "schema_version": 1,
        "source_files": [],
        "components": [],
        "connectors": [
            {"local_id": "J1", "pins": [{"pin": "1", "signal": "VIN"}, {"pin": "2", "signal": "GND"}]}
        ],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", a)
    return a


def _bom() -> dict:
    b = {
        "project_id": "proj",
        "schema_version": 1,
        "rows": [
            {
                "local_id": "J1",
                "refdes": "J1",
                "mpn": "PJ-037A",
                "package": "Barrel Jack",
                "footprint_hint": "Connector_BarrelJack:BarrelJack_CUI_PJ-037A_Horizontal",
                "symbol_hint": "Connector:Barrel_Jack",
                "confidence": 0.9,
            }
        ],
    }
    schema.validate("bom", b)
    return b


def _nets() -> dict:
    n = {
        "project_id": "proj",
        "schema_version": 1,
        "nets": [
            {"name": "VIN", "class": "Power_Bulk", "members": [{"refdes": "J1", "pin": "1"}]},
            {"name": "GND", "class": "Power_Bulk", "members": [{"refdes": "J1", "pin": "2"}]},
        ],
    }
    schema.validate("nets", n)
    return n


def test_emit_produces_yaml_to_kicad_compatible_structure() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _design_artifact(), _project_config())
    # Top-level keys match what yaml_to_kicad.py expects.
    assert set(hdm.keys()) >= {"project", "net_classes", "boundaries", "components", "nets"}
    # Component structure.
    assert "J1" in hdm["components"]
    j1 = hdm["components"]["J1"]
    assert j1["value"] == "PJ-037A"
    assert j1["footprint"] == "Connector_BarrelJack:BarrelJack_CUI_PJ-037A_Horizontal"
    assert j1["lib_symbol"] == "Connector:Barrel_Jack"
    assert set(j1["placement"]) == {"x", "y", "rot", "side"}
    # Net structure: pads is list of [refdes, pin] pairs.
    assert hdm["nets"]["GND"]["class"] == "Power_Bulk"
    # The design's own pad comes first; the default test-point policy appends
    # a probe point to every power net (see test_stage5_test_points.py).
    assert hdm["nets"]["GND"]["pads"][0] == ["J1", "2"]


def test_coverage_match_overrides_bom_hints() -> None:
    coverage = {
        "project_id": "proj",
        "schema_version": 1,
        "generated_at": "2026-04-20T00:00:00+00:00",
        "library_roots": {"symbols": "/x", "footprints": "/y"},
        "rows": [
            {
                "local_id": "J1",
                "mpn": "PJ-037A",
                "status": "hit",
                "symbol_match": {"lib": "Connector", "name": "Barrel_Jack_Switch", "match_type": "exact"},
                "footprint_match": {"lib": "Connector_BarrelJack", "name": "BarrelJack_CUI_PJ-079BH_Horizontal", "match_type": "exact"},
            }
        ],
        "summary": {"total": 1, "hit": 1, "needs_variant": 0, "miss": 0},
    }
    schema.validate("coverage_report", coverage)

    hdm, _, _ = s5.emit(_bom(), _nets(), _design_artifact(), _project_config(), coverage=coverage)
    # Coverage-confirmed names should win over bom hints.
    assert hdm["components"]["J1"]["lib_symbol"] == "Connector:Barrel_Jack_Switch"
    assert hdm["components"]["J1"]["footprint"] == "Connector_BarrelJack:BarrelJack_CUI_PJ-079BH_Horizontal"


def test_pin_map_generated_for_connectors() -> None:
    hdm, _, _ = s5.emit(_bom(), _nets(), _design_artifact(), _project_config())
    assert hdm["components"]["J1"]["pin_map"] == {"VIN": "1", "GND": "2"}


def test_missing_project_yaml_writes_template_and_raises(tmp_path: Path) -> None:
    expected = tmp_path / "project.yaml"
    with pytest.raises(s5.MissingProjectConfigError, match="project.yaml is required"):
        s5.ensure_project_config(expected, "test-proj")
    template = tmp_path / "project.yaml.template"
    assert template.exists()
    # Template must be valid YAML and contain the project_id.
    tpl = yaml.safe_load(template.read_text())
    assert tpl["project"]["name"] == "test-proj"


def test_run_writes_valid_yaml(tmp_path: Path) -> None:
    import json
    # Lay out input files.
    bom_p = tmp_path / "bom.json"; bom_p.write_text(json.dumps(_bom()))
    nets_p = tmp_path / "nets.json"; nets_p.write_text(json.dumps(_nets()))
    da_p = tmp_path / "design_artifact.json"; da_p.write_text(json.dumps(_design_artifact()))
    proj_p = tmp_path / "project.yaml"; proj_p.write_text(yaml.safe_dump(_project_config()))
    out = tmp_path / "hdm.yaml"
    s5.run(bom_p, nets_p, da_p, proj_p, out)
    loaded = yaml.safe_load(out.read_text())
    assert loaded["project"]["name"] == "TestProj"
    assert loaded["components"]["J1"]["value"] == "PJ-037A"


def test_a_not_placed_row_becomes_no_copper_and_is_not_dropped_silently() -> None:
    """The other half of doctor's not_placed flag.

    If Stage 5 did not honor it, resolve_footprint would substitute a
    placeholder header for the flag string — copper under a part that must
    have none, on a board doctor just declared clean. The row stays in
    bom.json so ordering still sees it; the HDM records who was skipped so a
    diff against the BOM does not show rows that vanished with nothing
    saying why.
    """
    bom = _bom()
    bom["rows"].append({
        "local_id": "BAT_RTC",
        "refdes": "BAT_RTC",
        "mpn": "ML1220",
        "package": "not_placed",
        "confidence": 1.0,
    })
    schema.validate("bom", bom)

    hdm, _, fresolutions = s5.emit(bom, _nets(), _design_artifact(), _project_config())

    assert "BAT_RTC" not in hdm["components"]
    assert "BAT_RTC" not in fresolutions
    assert hdm["not_placed"] == ["BAT_RTC"]
    # The placed part is untouched.
    assert "J1" in hdm["components"]
