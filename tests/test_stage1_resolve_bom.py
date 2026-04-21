"""Unit tests for pipeline.stage1_resolve_bom with a stub adapter."""

from __future__ import annotations

from pathlib import Path

from blpl.core import schema, stage1_resolve_bom as s1


class _StubAdapter:
    provider = "stub"
    model = "stub-1"

    def __init__(self, response: dict):
        self._response = response
        self.last_schema: dict | None = None

    def complete_json(
        self, system: str, user: str, output_schema: dict, model: str | None = None
    ) -> dict:
        self.last_schema = output_schema
        return self._response


def _artifact() -> dict:
    a = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "U1",
                "description": "Zynq MPSoC",
                "part_hint": "XAZU1EG",
                "package_hint": "BGA-484",
            }
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", a)
    return a


def test_resolve_strips_nulls_and_validates(tmp_path: Path) -> None:
    adapter = _StubAdapter(
        {
            "rows": [
                {
                    "local_id": "U1",
                    "mpn": "XAZU1EG-1SBVA484I",
                    "manufacturer": "AMD/Xilinx",
                    "package": "BGA-484_19x19mm_P0.8mm",
                    "pin_count": 484,
                    "datasheet_url": None,
                    "description": "Zynq UltraScale+",
                    "role": None,
                    "symbol_hint": None,
                    "footprint_hint": "Package_BGA:BGA-484_19x19mm_P0.8mm",
                    "confidence": 0.95,
                    "notes": None,
                }
            ]
        }
    )
    bom = s1.resolve(_artifact(), adapter=adapter)
    schema.validate("bom", bom)
    (row,) = bom["rows"]
    assert row["mpn"] == "XAZU1EG-1SBVA484I"
    assert row["confidence"] == 0.95
    assert "datasheet_url" not in row  # null stripped
    assert "symbol_hint" not in row


def test_low_confidence_rows_reported() -> None:
    bom = {
        "project_id": "p",
        "schema_version": 1,
        "rows": [
            {"local_id": "A", "mpn": "X", "package": "Y", "confidence": 1.0},
            {"local_id": "B", "mpn": "X", "package": "Y", "confidence": 0.5},
            {"local_id": "C", "mpn": "X", "package": "Y", "confidence": 0.8},
        ],
    }
    low = s1.low_confidence_rows(bom, threshold=0.9)
    assert {r["local_id"] for r in low} == {"B", "C"}
