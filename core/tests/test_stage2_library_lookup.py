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
    # Was "fuzzy" — the MPN-containment pass now runs before token fuzzing and
    # catches this case first. The test's point survives: a wrong library in
    # the hint does not cost the match.
    assert row["symbol_match"]["match_type"] in ("mpn", "fuzzy")
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


# --- the full search path, and matching by MPN -------------------------------


def _write_symdir(root, lib, name):
    d = root / f"{lib}.kicad_symdir"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.kicad_sym").write_text('(kicad_symbol_lib (symbol "%s"))' % name)


def _bom_row(local_id, mpn, sym_hint=None):
    return {
        "local_id": local_id, "mpn": mpn, "package": "pkg", "confidence": 0.9,
        **({"symbol_hint": sym_hint} if sym_hint else {}),
    }


def _run_multiroot(tmp_path, rows, project_dir=None):
    import json
    from blpl.core import stage2_library_lookup as s2
    bom = {"project_id": "p", "schema_version": 1, "rows": rows}
    bom_path = tmp_path / "bom.json"
    bom_path.write_text(json.dumps(bom))
    out = tmp_path / "coverage.json"
    stock_sym = tmp_path / "stock-sym"; stock_sym.mkdir(exist_ok=True)
    stock_fp = tmp_path / "stock-fp"; stock_fp.mkdir(exist_ok=True)
    return s2.run(bom_path, stock_sym, stock_fp, out, project_dir=project_dir)


def test_a_shared_library_symbol_is_coverage_not_a_gap(tmp_path, monkeypatch):
    """Coverage must see every root Stage 5 resolves against. Stock-only
    coverage left six installed symbols invisible while the LLM's paraphrased
    hints sent each of those parts to a placeholder — coverage is what
    OUTRANKS the hint, so a library coverage cannot see is a library the
    pipeline cannot use."""
    proj = tmp_path / "proj"; proj.mkdir()
    shared = tmp_path / "modules"
    monkeypatch.setenv("BLPL_MODULES_ROOT", str(shared))
    _write_symdir(shared / "vendor", "InvenSense", "ICM-42670-P")

    report = _run_multiroot(tmp_path, [_bom_row("U_IMU", "ICM-42670-P", "Sensor_Motion:ICM-42670-P")],
                  project_dir=proj)
    (row,) = report["rows"]

    assert row["symbol_match"] is not None
    assert row["symbol_match"]["lib"] == "InvenSense"


def test_an_mpn_inside_a_longer_library_name_matches(tmp_path, monkeypatch):
    """Token-set Jaccard cannot see that BGS12P2L6 names the same silicon as
    BGS12P2L6E6327XTSA1 — one token each, zero overlap. Containment can."""
    proj = tmp_path / "proj"; proj.mkdir()
    shared = tmp_path / "modules"
    monkeypatch.setenv("BLPL_MODULES_ROOT", str(shared))
    _write_symdir(shared / "vendor", "Infineon", "BGS12P2L6E6327XTSA1")

    report = _run_multiroot(tmp_path, [_bom_row("SW1", "BGS12P2L6", "RF_Switch:BGS12P2L6")],
                  project_dir=proj)
    (row,) = report["rows"]

    assert row["symbol_match"] is not None
    assert row["symbol_match"]["name"] == "BGS12P2L6E6327XTSA1"
    assert row["symbol_match"]["match_type"] == "mpn"


def test_a_short_mpn_does_not_containment_match(tmp_path, monkeypatch):
    """Six characters minimum: every part a manufacturer makes shares its
    first few, and 'BGS12' matching every BGS switch would reintroduce the
    wrong-part hazard this exists to avoid."""
    proj = tmp_path / "proj"; proj.mkdir()
    shared = tmp_path / "modules"
    monkeypatch.setenv("BLPL_MODULES_ROOT", str(shared))
    _write_symdir(shared / "vendor", "Infineon", "BGS12P2L6E6327XTSA1")

    report = _run_multiroot(tmp_path, [_bom_row("SW9", "BGS12")], project_dir=proj)
    (row,) = report["rows"]

    assert row["symbol_match"] is None


def test_flat_symbol_files_are_indexed_too(tmp_path, monkeypatch):
    """Stage 3 GENERATES flat <Lib>.kicad_sym files and symbol_resolution has
    always resolved them; the index only ever read the directory layout, so
    every generated symbol was re-reported as a gap forever."""
    proj = tmp_path / "proj"
    gen = proj / "generated" / "symbols"
    gen.mkdir(parents=True)
    (gen / "Generated.kicad_sym").write_text(
        '(kicad_symbol_lib (symbol "MYPART123") (symbol "MYPART123_0_1"))'
    )

    report = _run_multiroot(tmp_path, [_bom_row("U9", "MYPART123")], project_dir=proj)
    (row,) = report["rows"]

    assert row["symbol_match"] is not None
    assert row["symbol_match"]["name"] == "MYPART123"


def test_a_not_placed_row_is_not_a_footprint_miss(tmp_path):
    """A not_placed part must match no footprint — any match would be wrong
    copper — so 'no match' is its correct state, not a miss. Counting it made
    stage2 exit nonzero and halt the run over a row nobody can act on."""
    import json
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / ".pipeline" / "design_artifact.deterministic.json").write_text(
        json.dumps({"components": [
            {"local_id": "BAT_RTC", "package_hint": "not_placed",
             "symbol_hint": "Device:Battery_Cell"},
        ]})
    )
    syms = tmp_path / "syms"
    (syms / "Device.kicad_symdir").mkdir(parents=True)
    (syms / "Device.kicad_symdir" / "Battery_Cell.kicad_sym").write_text("(symbol)")
    fps = tmp_path / "fps"
    fps.mkdir()
    bom_path = tmp_path / "bom.json"
    bom_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "rows": [{"local_id": "BAT_RTC", "mpn": "ML1220", "package": "not_placed",
                  "symbol_hint": "Device:Battery_Cell"}],
    }))
    report = stage2_library_lookup.run(bom_path, syms, fps, tmp_path / "cov.json", project_dir=proj)
    (row,) = report["rows"]
    assert row["status"] == "hit"
    assert "not_placed" in row["footprint_query"]


def test_an_unpinned_connector_symbol_is_not_a_miss(tmp_path):
    """A connector's symbol is generated by Stage 3 from its pinout, so a
    stock search measures nothing — unless the designer pinned one, which is
    a claim and is held to it."""
    import json
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / ".pipeline" / "design_artifact.deterministic.json").write_text(
        json.dumps({"components": [
            {"local_id": "J_SIM", "symbol_hint": "Molex:1042240820"},
        ]})
    )
    syms = tmp_path / "syms"
    syms.mkdir()
    fps = tmp_path / "fps"
    (fps / "Connector_FFC-FPC.pretty").mkdir(parents=True)
    (fps / "Connector_FFC-FPC.pretty" / "FPC_40P.kicad_mod").write_text("(footprint)")
    bom_path = tmp_path / "bom.json"
    bom_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "rows": [
            {"local_id": "J_DISP", "mpn": "IMSA-9631S", "package": "pkg",
             "symbol_hint": "Connector_FFC-FPC:Conn_FFC-FPC_40P",  # LLM invention
             "footprint_hint": "Connector_FFC-FPC:FPC_40P"},
            {"local_id": "J_SIM", "mpn": "1042240820", "package": "pkg",
             "symbol_hint": "Molex:1042240820",  # explicit pin: held to it
             "footprint_hint": "Connector_FFC-FPC:FPC_40P"},
        ],
    }))
    report = stage2_library_lookup.run(bom_path, syms, fps, tmp_path / "cov.json", project_dir=proj)
    rows = {r["local_id"]: r for r in report["rows"]}
    assert rows["J_DISP"]["status"] == "hit"
    assert "Stage 3" in rows["J_DISP"]["symbol_query"]
    # The pinned connector's symbol does not exist in the (empty) libraries:
    # the claim is checked and it fails.
    assert rows["J_SIM"]["status"] == "miss"
