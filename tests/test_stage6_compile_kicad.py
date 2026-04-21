"""Smoke tests for pipeline.stage6_compile_kicad — end-to-end from HDM to KiCad outputs."""

from __future__ import annotations

from pathlib import Path

import yaml

from blpl.core import stage6_compile_kicad as s6


def _minimal_hdm(name: str = "Smoke Test") -> dict:
    return {
        "project": {
            "name": name,
            "board_id": "SMK-1",
            "dimensions": [50, 40],
            "stackup": {"layers": 2, "thickness": 1.6, "finish": "HASL"},
        },
        "net_classes": {
            "Default": {"trace_width": 0.2, "clearance": 0.15, "via_dia": 0.6, "via_drill": 0.3}
        },
        "boundaries": {
            "board_outline": {
                "type": "rect",
                "start": [0, 0],
                "end": [50, 40],
                "layer": "Edge.Cuts",
                "width": 0.1,
            }
        },
        "components": {
            "J1": {
                "value": "PJ-063AH",
                "footprint": "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
                "lib_symbol": "Connector:Barrel_Jack",
                "placement": {"x": 10, "y": 10, "rot": 0, "side": "top"},
            }
        },
        "nets": {
            "VIN": {"class": "Default", "pads": [["J1", "1"]]},
            "GND": {"class": "Default", "pads": [["J1", "2"]]},
        },
    }


def test_sanitize_filename_handles_spaces_and_specials() -> None:
    assert s6._sanitize_filename("Example Base Station") == "Example_Base_Station"
    assert s6._sanitize_filename("foo/bar?baz") == "foo_bar_baz"
    assert s6._sanitize_filename("") == "project"


def test_run_produces_sch_pcb_and_pro_files(tmp_path: Path) -> None:
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out_dir = tmp_path / "out"
    results = s6.run(hdm_path, out_dir, stamp="2026-04-20_120000Z")
    sch = results["sch"]
    pcb = results["pcb"]
    pro = results["pro"]
    assert sch.exists() and sch.stat().st_size > 0
    assert pcb.exists() and pcb.stat().st_size > 0
    assert pro.exists() and pro.stat().st_size > 0
    assert sch.read_text().startswith("(kicad_sch")
    assert pcb.read_text().startswith("(kicad_pcb")
    import json
    assert "net_settings" in json.loads(pro.read_text())


def test_run_filenames_carry_shared_timestamp(tmp_path: Path) -> None:
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out_dir = tmp_path / "out"
    stamp = "2026-04-20_120000Z"
    results = s6.run(hdm_path, out_dir, stamp=stamp)
    # All three files share a single basename so KiCad treats them as one project.
    assert results["base"] == f"Smoke_Test_{stamp}"
    assert results["sch"].name == f"Smoke_Test_{stamp}.kicad_sch"
    assert results["pcb"].name == f"Smoke_Test_{stamp}.kicad_pcb"
    assert results["pro"].name == f"Smoke_Test_{stamp}.kicad_pro"


def test_run_auto_generates_stamp_when_omitted(tmp_path: Path) -> None:
    import re as _re
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    results = s6.run(hdm_path, tmp_path / "out")
    # Auto-generated stamp matches YYYY-MM-DD_HHMMSSZ.
    assert _re.search(r"_\d{4}-\d{2}-\d{2}_\d{6}Z$", results["base"])
