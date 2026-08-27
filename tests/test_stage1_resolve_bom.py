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


def test_an_explicit_library_reference_survives_the_llm(tmp_path: Path) -> None:
    """`Lib:Name` in the Package column is the designer naming the exact
    footprint — not a hint to improve on. The LLM sees it in its prompt and
    still paraphrases: on a real board `Seeed:Wio-LR2021_V1` came back as
    `RF_Module:Seeed_Wio-LR2021_V1`, a plausible stock spelling that exists
    nowhere, and ten resolved parts were emitted as placeholder headers."""
    artifact = {
        "project_id": "proj",
        "schema_version": 1,
        "components": [
            {
                "local_id": "U_LORA",
                "description": "LoRa module",
                "part_hint": "100058045",
                "package_hint": "Seeed:Wio-LR2021_V1",
            },
            {
                "local_id": "R1",
                "description": "resistor",
                "package_hint": "0402",
            },
        ],
        "connectors": [],
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", artifact)
    adapter = _StubAdapter({"rows": [
        {"local_id": "U_LORA", "mpn": "100058045", "manufacturer": "Seeed",
         "package": "Module", "pin_count": 22, "datasheet_url": None,
         "description": "LoRa", "role": None, "symbol_hint": "RF_Module:LR2021",
         "footprint_hint": "RF_Module:Seeed_Wio-LR2021_V1",  # the paraphrase
         "confidence": 0.9, "notes": None, "value": None, "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
        {"local_id": "R1", "mpn": "RC0402", "manufacturer": "Yageo",
         "package": "0402", "pin_count": 2, "datasheet_url": None,
         "description": "resistor", "role": None, "symbol_hint": "Device:R",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric",  # canonicalised hint: the LLM's job
         "confidence": 0.9, "notes": None, "value": "10k", "tolerance": None,
         "voltage_v": None, "power_w": None, "dielectric": None, "safety_class": None},
    ]})

    bom = s1.resolve(artifact, adapter=adapter, synthesize_connectors=False)
    rows = {r["local_id"]: r for r in bom["rows"]}

    # The explicit reference is pinned back over the paraphrase…
    assert rows["U_LORA"]["footprint_hint"] == "Seeed:Wio-LR2021_V1"
    # …while a bare hint stays the LLM's to canonicalise.
    assert rows["R1"]["footprint_hint"] == "Resistor_SMD:R_0402_1005Metric"
