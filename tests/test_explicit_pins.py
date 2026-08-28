"""The design doc's explicit pins flow without the LLM stage.

bom.json is Stage 1's output and Stage 1 is the LLM stage — so before this
overlay existed, an explicit Symbol or Package cell edited in the design doc
reached nothing until a paid rerun regenerated the file. The user edits one
table; that table must be live.
"""

from __future__ import annotations

import json
from pathlib import Path

from blpl.core import explicit_pins


def _artifact(tmp_path: Path, components: list[dict]) -> None:
    p = tmp_path / ".pipeline"
    p.mkdir(exist_ok=True)
    (p / "design_artifact.deterministic.json").write_text(
        json.dumps({"components": components}), encoding="utf-8"
    )


def test_doc_pins_override_stale_bom_hints(tmp_path: Path) -> None:
    _artifact(tmp_path, [
        {"local_id": "U_BLE", "symbol_hint": "proj:AN54LV-U15"},
        {"local_id": "U_LORA", "package_hint": "Seeed:Wio-LR2021_V1"},
    ])
    rows = [
        {"local_id": "U_BLE", "symbol_hint": "RF_Module:MDBT50Q-1MV2"},
        {"local_id": "U_LORA", "footprint_hint": "RF_Module:Seeed_LR2021"},
        {"local_id": "R1", "symbol_hint": "Device:R"},
    ]
    changed = explicit_pins.apply(rows, tmp_path)
    assert changed == 2
    assert rows[0]["symbol_hint"] == "proj:AN54LV-U15"
    assert rows[1]["footprint_hint"] == "Seeed:Wio-LR2021_V1"
    assert rows[2]["symbol_hint"] == "Device:R"  # unpinned rows untouched


def test_not_placed_is_restored_over_an_llm_paraphrase(tmp_path: Path) -> None:
    _artifact(tmp_path, [{"local_id": "BAT_RTC", "package_hint": "not_placed"}])
    rows = [{"local_id": "BAT_RTC", "package": "Coin Cell 1220",
             "footprint_hint": "Battery:BatteryHolder_1220"}]
    explicit_pins.apply(rows, tmp_path)
    assert rows[0]["package"] == "not_placed"
    assert "footprint_hint" not in rows[0]


def test_bare_hints_are_not_pins(tmp_path: Path) -> None:
    """No colon, no claim — canonicalising '0402' stays the LLM's job."""
    _artifact(tmp_path, [{"local_id": "R1", "package_hint": "0402"}])
    rows = [{"local_id": "R1", "footprint_hint": "Resistor_SMD:R_0402_1005Metric"}]
    assert explicit_pins.apply(rows, tmp_path) == 0
    assert rows[0]["footprint_hint"] == "Resistor_SMD:R_0402_1005Metric"


def test_a_missing_artifact_is_a_no_op(tmp_path: Path) -> None:
    """The overlay must never be the thing that breaks a run."""
    rows = [{"local_id": "U1", "symbol_hint": "Device:R"}]
    assert explicit_pins.apply(rows, tmp_path) == 0
    assert explicit_pins.apply(rows, None) == 0
