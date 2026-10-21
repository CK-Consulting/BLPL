"""The BOM as CSV: everything on the board, or only what is bought."""

from __future__ import annotations

import csv
import io

import pytest

from blpl.core import bom_export

HDM = {"components": {
    "R10": {"value": "470", "mpn": "RC0402FR-07470RL", "manufacturer": "Yageo",
            "footprint": "Resistor_SMD:R_0402_1005Metric"},
    "R2": {"value": "470", "mpn": "RC0402FR-07470RL", "manufacturer": "Yageo",
           "footprint": "Resistor_SMD:R_0402_1005Metric"},
    "U1": {"value": "XIAO", "mpn": "102010", "manufacturer": "Seeed", "footprint": "Seeed:XIAO",
           "description": "=HYPERLINK(\"x\")"},
    "J_DBG": {"value": "TC2030-NL-FP", "mpn": "TC2030-NL-FP", "footprint": "Connector:TC2030",
              "assembly": "footprint_only"},
    "R9": {"value": "0", "mpn": "RC0402", "footprint": "Resistor_SMD:R_0402_1005Metric", "assembly": "dnp"},
    "TP1": {"value": "TP_GND", "footprint": "TestPoint:TestPoint_Pad_D1.0mm", "synthesized": "test_point"},
    "FID1": {"value": "Fiducial", "footprint": "Fiducial:Fiducial_1mm_Mask2mm"},
}}


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_all_lists_every_part_once_with_how_it_is_assembled():
    rows = _rows(bom_export.to_csv(bom_export.parts(None, HDM), "all"))
    assert [r["Designator"] for r in rows] == ["FID1", "J_DBG", "R2", "R9", "R10", "TP1", "U1"]
    assembly = {r["Designator"]: r["Assembly"] for r in rows}
    assert assembly == {"FID1": "fitted", "J_DBG": "footprint_only", "R2": "fitted", "R9": "dnp",
                        "R10": "fitted", "TP1": "fitted", "U1": "fitted"}


def test_purchase_keeps_only_what_a_supplier_ships_grouped_with_a_quantity():
    text = bom_export.to_csv(bom_export.parts(None, HDM), "purchase")
    assert text.splitlines()[0] == "MPN,MFG,QTY"
    assert [tuple(r.values()) for r in _rows(text)] == [
        ("102010", "Seeed", "1"), ("RC0402FR-07470RL", "Yageo", "2")]


def test_a_bought_part_with_no_mpn_still_gets_a_line_that_says_so():
    hdm = {"components": {
        "R5": {"value": "10k", "footprint": "Resistor_SMD:R_0402"},
        "R6": {"value": "10k", "footprint": "Resistor_SMD:R_0402"},
        "C1": {"value": "1u", "mpn": "GRM", "manufacturer": "Murata", "footprint": "C_0402"},
        "C2": {"value": "1u", "mpn": "grm", "manufacturer": "MURATA", "footprint": "C_0402"},
    }}
    rows = [tuple(r.values()) for r in _rows(bom_export.to_csv(bom_export.parts(None, hdm), "purchase"))]
    assert rows == [("GRM", "Murata", "2"), ("NO MPN: 10k Resistor_SMD:R_0402 (R5,R6)", "", "2")]


def test_a_cell_a_spreadsheet_would_run_is_written_as_text():
    rows = _rows(bom_export.to_csv(bom_export.parts(None, HDM), "all"))
    assert next(r for r in rows if r["Designator"] == "U1")["Description"].startswith("'=")


def test_before_stage5_the_stage1_bom_is_used_and_everything_counts_as_bought():
    bom = {"rows": [{"local_id": "U1", "refdes": "U1", "mpn": "102010", "package": "XIAO"},
                    {"local_id": "C1", "mpn": "GRM", "footprint_hint": "Capacitor_SMD:C_0402"}]}
    records = bom_export.parts(bom, None)
    assert [p["ref"] for p in records] == ["C1", "U1"]
    assert len(_rows(bom_export.to_csv(records, "purchase"))) == 2


def test_an_unknown_purpose_is_refused():
    with pytest.raises(ValueError):
        bom_export.to_csv([], "quotes")
