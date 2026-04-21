"""Unit tests for pipeline.stage2_library_lookup.

Uses a synthetic KiCad library tree under tmp_path so the tests are hermetic
and don't depend on the specific revision of the kicad-symbols / kicad-footprints
submodules checked out in this repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.core import stage2_library_lookup


def _write_symbol(symbols_root: Path, lib: str, name: str) -> None:
    d = symbols_root / f"{lib}.kicad_symdir"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.kicad_sym").write_text(
        f'(kicad_symbol_lib (version 20251024) (symbol "{name}"))\n'
    )


def _write_footprint(footprints_root: Path, lib: str, name: str) -> None:
    d = footprints_root / f"{lib}.pretty"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.kicad_mod").write_text(f'(footprint "{name}")\n')


def _make_bom(project_id: str, rows: list[dict]) -> dict:
    return {"project_id": project_id, "schema_version": 1, "rows": rows}


@pytest.fixture()
def libs(tmp_path: Path) -> tuple[Path, Path]:
    symbols = tmp_path / "kicad-symbols"
    footprints = tmp_path / "kicad-footprints"
    _write_symbol(symbols, "Power_Management", "TPS65086")
    _write_symbol(symbols, "FPGA_Xilinx", "XC7Z010")
    _write_symbol(symbols, "Connector", "Barrel_Jack")
    _write_footprint(footprints, "Package_BGA", "BGA-484_19x19mm_P0.8mm")
    _write_footprint(footprints, "Package_DFN_QFN", "QFN-48-1EP_7x7mm_P0.5mm")
    _write_footprint(
        footprints, "Connector_BarrelJack", "BarrelJack_CUI_PJ-037A_Horizontal"
    )
    return symbols, footprints


def _run(tmp_path: Path, libs: tuple[Path, Path], bom: dict) -> dict:
    bom_path = tmp_path / "bom.json"
    bom_path.write_text(json.dumps(bom))
    out_path = tmp_path / "coverage_report.json"
    return stage2_library_lookup.run(
        bom_path=bom_path,
        symbols_root=libs[0],
        footprints_root=libs[1],
        output_path=out_path,
    )


def test_exact_hit_on_both_symbol_and_footprint(tmp_path: Path, libs: tuple[Path, Path]) -> None:
    bom = _make_bom(
        "proj",
        [
            {
                "local_id": "U2",
                "mpn": "TPS65086RSMR",
                "package": "QFN-48-1EP_7x7mm_P0.5mm",
                "symbol_hint": "Power_Management:TPS65086",
                "footprint_hint": "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
            }
        ],
    )
    report = _run(tmp_path, libs, bom)
    row = report["rows"][0]
    assert row["status"] == "hit"
    assert row["symbol_match"]["match_type"] == "exact"
    assert row["symbol_match"]["lib"] == "Power_Management"
    assert row["footprint_match"]["match_type"] == "exact"
    assert report["summary"] == {"total": 1, "hit": 1, "needs_variant": 0, "miss": 0}


def test_fuzzy_symbol_when_hint_library_wrong(tmp_path: Path, libs: tuple[Path, Path]) -> None:
    """Symbol hint says 'MCU_Xilinx' (which doesn't exist); fuzzy should find FPGA_Xilinx:XC7Z010."""
    bom = _make_bom(
        "proj",
        [
            {
                "local_id": "U1",
                "mpn": "XC7Z010-1CLG400C",
                "package": "BGA-484_19x19mm_P0.8mm",
                "symbol_hint": "MCU_Xilinx:XC7Z010",
                "footprint_hint": "Package_BGA:BGA-484_19x19mm_P0.8mm",
            }
        ],
    )
    report = _run(tmp_path, libs, bom)
    row = report["rows"][0]
    assert row["status"] == "needs_variant"
    assert row["symbol_match"]["match_type"] == "fuzzy"
    assert row["symbol_match"]["lib"] == "FPGA_Xilinx"
    assert row["symbol_match"]["name"] == "XC7Z010"
    assert row["footprint_match"]["match_type"] == "exact"


def test_miss_when_nothing_matches(tmp_path: Path, libs: tuple[Path, Path]) -> None:
    bom = _make_bom(
        "proj",
        [
            {
                "local_id": "U9",
                "mpn": "TOTALLY_FAKE_PART_12345",
                "package": "NONEXISTENT_PACKAGE_99",
                "symbol_hint": "NoSuchLib:NoSuchPart",
                "footprint_hint": "NoSuchLib:NoSuchFP",
            }
        ],
    )
    report = _run(tmp_path, libs, bom)
    row = report["rows"][0]
    assert row["status"] == "miss"
    assert row["symbol_match"] is None
    assert row["footprint_match"] is None
    assert report["summary"]["miss"] == 1


def test_report_is_schema_valid(tmp_path: Path, libs: tuple[Path, Path]) -> None:
    """The written report must validate against coverage_report.v1.json."""
    from blpl.core import schema

    bom = _make_bom(
        "proj",
        [
            {
                "local_id": "U2",
                "mpn": "TPS65086RSMR",
                "package": "QFN-48-1EP_7x7mm_P0.5mm",
                "symbol_hint": "Power_Management:TPS65086",
                "footprint_hint": "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm",
            },
        ],
    )
    report = _run(tmp_path, libs, bom)
    schema.validate("coverage_report", report)
    written = json.loads((tmp_path / "coverage_report.json").read_text())
    schema.validate("coverage_report", written)
