"""Unit tests for pipeline.stage3_generate."""

from __future__ import annotations

import json
from pathlib import Path

from blpl.core import schema, stage3_generate as s3


def _write_bom(path: Path, rows: list[dict]) -> None:
    bom = {"project_id": "proj", "schema_version": 1, "rows": rows}
    schema.validate("bom", bom)
    path.write_text(json.dumps(bom))


def _write_coverage(path: Path, rows: list[dict]) -> None:
    cov = {
        "project_id": "proj",
        "schema_version": 1,
        "generated_at": "2026-04-20T00:00:00+00:00",
        "library_roots": {"symbols": "/x", "footprints": "/y"},
        "rows": rows,
        "summary": {
            "total": len(rows),
            "hit": sum(1 for r in rows if r["status"] == "hit"),
            "needs_variant": sum(1 for r in rows if r["status"] == "needs_variant"),
            "miss": sum(1 for r in rows if r["status"] == "miss"),
        },
    }
    schema.validate("coverage_report", cov)
    path.write_text(json.dumps(cov))


def _setup(tmp_path: Path, bom_rows: list[dict], cov_rows: list[dict]) -> tuple[Path, Path, Path]:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    bom_path = proj / ".pipeline" / "bom.json"
    cov_path = proj / ".pipeline" / "coverage_report.json"
    _write_bom(bom_path, bom_rows)
    _write_coverage(cov_path, cov_rows)
    return proj, bom_path, cov_path


def test_hits_with_existing_pin_map_produce_no_gaps(tmp_path: Path) -> None:
    """A row with coverage hits AND an existing pin_map is fully resolved."""
    bom_rows = [
        {
            "local_id": "U1",
            "mpn": "X",
            "package": "Y",
            "pin_count": 4,
            "confidence": 1.0,
            "pin_map": {"A": "1", "B": "2"},
        }
    ]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "X",
            "status": "hit",
            "symbol_match": {"lib": "L", "name": "N", "match_type": "exact"},
            "footprint_match": {"lib": "L", "name": "N", "match_type": "exact"},
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=False)
    assert out["gaps"] == []


def test_hits_without_pin_map_emit_specific_pin_map_gap(tmp_path: Path) -> None:
    """An unclassifiable row with no pin_map must produce an interactive prompt."""
    bom_rows = [{"local_id": "U1", "mpn": "X", "package": "Y", "pin_count": 4, "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "X",
            "status": "hit",
            "symbol_match": {"lib": "L", "name": "N", "match_type": "exact"},
            "footprint_match": {"lib": "L", "name": "N", "match_type": "exact"},
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=False)
    assert len(out["gaps"]) == 1
    (g,) = out["gaps"]
    assert g["kind"] == "pin_map" and not g["auto_generated"]
    assert g["pin_map_resolution"]["source"] == "specific_needs_user"


def test_symbol_miss_emits_user_prompt_by_default(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "FAKE", "package": "Y", "pin_count": 4, "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "FAKE",
            "status": "miss",
            "symbol_match": None,
            "footprint_match": None,
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=False)
    kinds = {g["kind"] for g in out["gaps"]}
    assert kinds == {"symbol", "footprint", "pin_map"}
    # Symbol + footprint + pin_map gaps are all non-auto for an unclassified FAKE part.
    assert all(not g["auto_generated"] for g in out["gaps"])
    # Symbol/footprint prompts reference suggested_input_path; pin_map uses CLI instructions instead.
    for g in out["gaps"]:
        if g["kind"] in ("symbol", "footprint"):
            assert g["suggested_input_path"] in g["user_prompt"]


def test_classifier_auto_resolves_passive_and_writes_bom(tmp_path: Path) -> None:
    """A resistor row should get pin_map, symbol_hint, footprint_hint written back."""
    bom_rows = [
        {
            "local_id": "R1",
            "mpn": "RC0603FR-0710KL",
            "package": "0603",
            "description": "Resistor 10k 1% 0603",
            "confidence": 1.0,
        }
    ]
    cov_rows = [
        {
            "local_id": "R1",
            "mpn": "RC0603FR-0710KL",
            "status": "hit",
            "symbol_match": {"lib": "Device", "name": "R", "match_type": "exact"},
            "footprint_match": {
                "lib": "Resistor_SMD",
                "name": "R_0603_1608Metric",
                "match_type": "exact",
            },
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=False)
    # Exactly one auto-resolved pin_map gap.
    pm_gaps = [g for g in out["gaps"] if g["kind"] == "pin_map"]
    assert len(pm_gaps) == 1 and pm_gaps[0]["auto_generated"] is True
    assert pm_gaps[0]["pin_map_resolution"]["source"] == "passive_identity"
    # bom.json was updated in place.
    bom_after = json.loads(bom.read_text())
    row = bom_after["rows"][0]
    assert row["pin_map"] == {"1": "1", "2": "2"}
    assert row["pin_map_source"] == "passive_identity"
    assert row["symbol_hint"] == "Device:R"
    assert row["footprint_hint"] == "Resistor_SMD:R_0603_1608Metric"


def test_classifier_skips_when_pin_map_already_present(tmp_path: Path) -> None:
    """A row with a hand-authored pin_map is left alone — no duplicate audit entry."""
    bom_rows = [
        {
            "local_id": "R1",
            "mpn": "RC0603FR-0710KL",
            "package": "0603",
            "description": "Resistor 10k 1% 0603",
            "confidence": 1.0,
            "pin_map": {"A": "1", "B": "2"},
        }
    ]
    cov_rows = [
        {
            "local_id": "R1",
            "mpn": "RC0603FR-0710KL",
            "status": "hit",
            "symbol_match": {"lib": "Device", "name": "R", "match_type": "exact"},
            "footprint_match": {"lib": "Resistor_SMD", "name": "R_0603_1608Metric", "match_type": "exact"},
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=False)
    assert [g for g in out["gaps"] if g["kind"] == "pin_map"] == []
    # Hand-authored pin_map untouched.
    bom_after = json.loads(bom.read_text())
    assert bom_after["rows"][0]["pin_map"] == {"A": "1", "B": "2"}


def test_auto_generate_produces_symbol_file_when_pin_count_known(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "FAKEPART", "package": "Y", "pin_count": 6, "description": "test", "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "FAKEPART",
            "status": "miss",
            "symbol_match": None,
            "footprint_match": None,
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=True)

    symbol_gaps = [g for g in out["gaps"] if g["kind"] == "symbol"]
    (sg,) = symbol_gaps
    assert sg["auto_generated"] is True
    # The generated file actually exists.
    gen_rel = sg["generated_path"]
    gen_abs = Path.cwd() / gen_rel if not Path(gen_rel).is_absolute() else Path(gen_rel)
    assert gen_abs.exists()
    assert gen_abs.read_text().startswith("(kicad_symbol_lib")
    # Footprint gap is still a prompt (we never auto-gen footprints in v1).
    fp_gaps = [g for g in out["gaps"] if g["kind"] == "footprint"]
    (fpg,) = fp_gaps
    assert fpg["auto_generated"] is False


def test_auto_generate_without_pin_count_falls_through_to_prompt(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "FAKE", "package": "Y", "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "FAKE",
            "status": "miss",
            "symbol_match": None,
            "footprint_match": None,
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    out = s3.run(cov, bom, proj, auto_generate=True)
    symbol_gap = next(g for g in out["gaps"] if g["kind"] == "symbol")
    assert symbol_gap["auto_generated"] is False
    assert "Please provide a .kicad_sym" in symbol_gap["user_prompt"]


_SYMBOLS_ROOT = Path(__file__).resolve().parent.parent / "kicad-symbols"


def test_resolve_pin_map_from_lib_symbol_writes_bom(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "J_USB_C", "mpn": "10137062-00LF", "package": "USB-C Receptacle", "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "J_USB_C",
            "mpn": "10137062-00LF",
            "status": "miss",
            "symbol_match": None,
            "footprint_match": None,
        }
    ]
    proj, bom, _cov = _setup(tmp_path, bom_rows, cov_rows)
    result = s3.resolve_pin_map(
        local_id="J_USB_C",
        bom_path=bom,
        lib_symbol="Connector:USB_C_Receptacle",
        symbols_root=_SYMBOLS_ROOT,
    )
    assert result["persisted"] is True
    assert result["source"] == "connector_lookup"
    assert "GND" in result["pin_map"] and "VBUS" in result["pin_map"]
    # USB-C full has multi-pin GND/VBUS → warning surfaced.
    assert any("GND" in w for w in result["warnings"])

    bom_after = json.loads(bom.read_text())
    row = bom_after["rows"][0]
    assert row["pin_map_source"] == "connector_lookup"
    assert row["symbol_hint"] == "Connector:USB_C_Receptacle"
    assert "GND" in row["pin_map"]


def test_resolve_pin_map_dry_run_does_not_persist(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "J1", "mpn": "PJ-063AH", "package": "Barrel jack", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    before = bom.read_text()
    result = s3.resolve_pin_map(
        local_id="J1",
        bom_path=bom,
        lib_symbol="Connector:Barrel_Jack",
        symbols_root=_SYMBOLS_ROOT,
        dry_run=True,
    )
    assert result["persisted"] is False
    assert result["pin_map"] == {"1": "1", "2": "2"}
    assert bom.read_text() == before  # file untouched


def test_resolve_pin_map_from_csv_signal_first_header(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "CUSTOM", "package": "BGA", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    csv_path = tmp_path / "pinmap.csv"
    csv_path.write_text("signal,pin\nVCC,1\nGND,2\nDAT,3\n")
    result = s3.resolve_pin_map(local_id="U1", bom_path=bom, csv_path=csv_path)
    assert result["source"] == "user_provided"
    assert result["pin_map"] == {"VCC": "1", "GND": "2", "DAT": "3"}


def test_resolve_pin_map_from_csv_pin_first_header(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "CUSTOM", "package": "BGA", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    csv_path = tmp_path / "pinmap.csv"
    csv_path.write_text("pin,signal\n1,VCC\n2,GND\n")
    result = s3.resolve_pin_map(local_id="U1", bom_path=bom, csv_path=csv_path)
    assert result["pin_map"] == {"VCC": "1", "GND": "2"}


def test_resolve_pin_map_from_csv_without_header(tmp_path: Path) -> None:
    """Without a recognised header, assume signal,pin order."""
    bom_rows = [{"local_id": "U1", "mpn": "CUSTOM", "package": "BGA", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    csv_path = tmp_path / "pinmap.csv"
    csv_path.write_text("VCC,1\nGND,2\n# comment line\n\n")
    result = s3.resolve_pin_map(local_id="U1", bom_path=bom, csv_path=csv_path)
    assert result["pin_map"] == {"VCC": "1", "GND": "2"}


def test_resolve_pin_map_rejects_unknown_local_id(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "X", "package": "Y", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    import pytest
    with pytest.raises(s3.ResolvePinMapError) as exc:
        s3.resolve_pin_map(local_id="DOES_NOT_EXIST", bom_path=bom, lib_symbol="Connector:Barrel_Jack", symbols_root=_SYMBOLS_ROOT)
    assert "DOES_NOT_EXIST" in str(exc.value)


def test_resolve_pin_map_rejects_neither_or_both_sources(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "X", "package": "Y", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    import pytest
    with pytest.raises(s3.ResolvePinMapError):
        s3.resolve_pin_map(local_id="U1", bom_path=bom)  # neither
    csv_path = tmp_path / "p.csv"
    csv_path.write_text("a,b\n1,2\n")
    with pytest.raises(s3.ResolvePinMapError):
        s3.resolve_pin_map(
            local_id="U1", bom_path=bom, lib_symbol="Connector:Barrel_Jack", csv_path=csv_path
        )


def test_resolve_pin_map_lib_symbol_not_found_errors(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "X", "package": "Y", "confidence": 1.0}]
    proj, bom, _cov = _setup(tmp_path, bom_rows, [])
    import pytest
    with pytest.raises(s3.ResolvePinMapError) as exc:
        s3.resolve_pin_map(
            local_id="U1",
            bom_path=bom,
            lib_symbol="Nonexistent:Nothing",
            symbols_root=_SYMBOLS_ROOT,
        )
    assert "pin_map" in str(exc.value).lower() or "not found" in str(exc.value).lower()


def test_gaps_md_is_written_with_sections(tmp_path: Path) -> None:
    bom_rows = [{"local_id": "U1", "mpn": "F", "package": "Y", "pin_count": 2, "confidence": 1.0}]
    cov_rows = [
        {
            "local_id": "U1",
            "mpn": "F",
            "status": "miss",
            "symbol_match": None,
            "footprint_match": None,
        }
    ]
    proj, bom, cov = _setup(tmp_path, bom_rows, cov_rows)
    s3.run(cov, bom, proj, auto_generate=True)
    md = (proj / ".pipeline" / "gaps.md").read_text()
    assert "Auto-generated" in md
    assert "Needs your attention" in md  # for the footprint prompt


def test_a_doc_pinout_beats_the_classifier_and_survives_a_stage1_rerun(tmp_path):
    """pin_maps lived only in bom.json — Stage 1's output — so one Stage 1
    rerun silently destroyed every accumulated pin_map and Stage 3 asked for
    48 of them again by hand. The doc's pinout table is the durable source,
    and it also outranks the classifier: the 12-pin fuel gauge whose
    description says 'integrated sense resistor' was auto-resolved as a
    2-pin passive; its pinout table says otherwise."""
    import json
    proj = tmp_path
    (proj / ".pipeline").mkdir()
    (proj / ".pipeline" / "design_artifact.deterministic.json").write_text(json.dumps({
        "components": [],
        "connectors": [{"local_id": "U_GAUGE", "pins": [
            {"pin": str(i), "signal": s} for i, s in enumerate(
                ["GND", "SDA", "SCL", "VDD"], start=1)
        ]}],
    }))
    bom_path = proj / ".pipeline" / "bom.json"
    bom_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "rows": [{"local_id": "U_GAUGE", "mpn": "BQ27441DRZR-G1A", "package": "VSON-12",
                  "description": "Fuel gauge with integrated sense resistor",
                  "pin_count": 12}],
    }))
    cov_path = proj / ".pipeline" / "coverage_report.json"
    cov_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "library_roots": {"symbols": "s", "footprints": "f"},
        "rows": [{"local_id": "U_GAUGE", "mpn": "BQ27441DRZR-G1A", "status": "hit",
                  "symbol_match": {"lib": "BM", "name": "BQ27441", "match_type": "exact"},
                  "footprint_match": {"lib": "P", "name": "VSON12", "match_type": "exact"}}],
        "summary": {"total": 1, "hit": 1, "needs_variant": 0, "miss": 0},
    }))

    out = s3.run(cov_path, bom_path, proj)

    bom = json.loads(bom_path.read_text())
    row = bom["rows"][0]
    assert row["pin_map"] == {"GND": "1", "SDA": "2", "SCL": "3", "VDD": "4"}
    assert row["pin_map_source"] == "design_artifact"
    (gap,) = [g for g in out["gaps"] if g["local_id"] == "U_GAUGE"]
    assert gap["auto_generated"] is True


def test_a_not_placed_row_needs_no_pin_map(tmp_path):
    """Never on the board, never in the netlist — the DOC-011 exemption,
    applied where Stage 3 asks its own version of the question."""
    import json
    proj = tmp_path
    (proj / ".pipeline").mkdir()
    bom_path = proj / ".pipeline" / "bom.json"
    bom_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "rows": [{"local_id": "BAT_RTC", "mpn": "ML1220", "package": "not_placed"}],
    }))
    cov_path = proj / ".pipeline" / "coverage_report.json"
    cov_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "library_roots": {"symbols": "s", "footprints": "f"},
        "rows": [{"local_id": "BAT_RTC", "mpn": "ML1220", "status": "hit",
                  "symbol_match": {"lib": "D", "name": "Battery_Cell", "match_type": "exact"},
                  "footprint_match": None}],
        "summary": {"total": 1, "hit": 1, "needs_variant": 0, "miss": 0},
    }))

    out = s3.run(cov_path, bom_path, proj)
    assert [g for g in out["gaps"] if g["local_id"] == "BAT_RTC"] == []


def test_a_pinned_symbol_supplies_its_own_pin_map(tmp_path):
    """The gap prompt has always told the user to run resolve-pin-map
    --lib-symbol against the symbol they already named in the doc — a form
    with one field and one possible answer. Stage 3 fills it in itself."""
    import json
    proj = tmp_path
    (proj / ".pipeline").mkdir()
    lib = proj / "libraries" / "symbols" / "Infineon.kicad_symdir"
    lib.mkdir(parents=True)
    (lib / "BGS12P2L6E6327XTSA1.kicad_sym").write_text(
        '(kicad_symbol_lib\n'
        '(symbol "BGS12P2L6E6327XTSA1"\n'
        '\t(symbol "BGS12P2L6E6327XTSA1_1_1"\n'
        '\t\t(pin passive line (at 0 0 0) (length 2.54)\n'
        '\t\t\t(name "RF1") (number "1"))\n'
        '\t\t(pin passive line (at 0 0 0) (length 2.54)\n'
        '\t\t\t(name "GND") (number "2"))\n'
        '\t\t(pin passive line (at 0 0 0) (length 2.54)\n'
        '\t\t\t(name "RF2") (number "3"))\n'
        '\t)\n'
        ')\n'
        ')\n'
    )
    bom_path = proj / ".pipeline" / "bom.json"
    bom_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "rows": [{"local_id": "SW1", "mpn": "BGS12P2L6", "package": "TSLP-6",
                  "symbol_hint": "Infineon:BGS12P2L6E6327XTSA1", "pin_count": 3}],
    }))
    cov_path = proj / ".pipeline" / "coverage_report.json"
    cov_path.write_text(json.dumps({
        "project_id": "p", "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "library_roots": {"symbols": "s", "footprints": "f"},
        "rows": [{"local_id": "SW1", "mpn": "BGS12P2L6", "status": "hit",
                  "symbol_match": {"lib": "Infineon", "name": "BGS12P2L6E6327XTSA1", "match_type": "exact"},
                  "footprint_match": {"lib": "P", "name": "TSLP6", "match_type": "exact"}}],
        "summary": {"total": 1, "hit": 1, "needs_variant": 0, "miss": 0},
    }))

    s3.run(cov_path, bom_path, proj)

    row = json.loads(bom_path.read_text())["rows"][0]
    assert row["pin_map"] == {"RF1": "1", "GND": "2", "RF2": "3"}
    assert row["pin_map_source"] == "pinned_symbol"
