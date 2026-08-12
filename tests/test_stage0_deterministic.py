"""Unit tests for pipeline.stage0_deterministic."""

from __future__ import annotations

from pathlib import Path

from blpl.core import schema, stage0_deterministic as s0


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_bom_table_becomes_components(tmp_path: Path) -> None:
    bom_md = _write(
        tmp_path,
        "bom.md",
        "# BOM\n"
        "\n"
        "| Ref | Part Number | Description | Package | Manufacturer |\n"
        "|-----|-------------|-------------|---------|--------------|\n"
        "| U1  | XAZU1EG-1SBVA484I | Zynq MPSoC | BGA-484 | Xilinx |\n"
        "| J1  | PJ-037A | DC Jack | DC Barrel | CUI |\n",
    )
    artifact = s0.extract([bom_md])
    schema.validate("design_artifact", artifact)
    refs = {c["local_id"] for c in artifact["components"]}
    assert refs == {"U1", "J1"}
    u1 = next(c for c in artifact["components"] if c["local_id"] == "U1")
    assert u1["part_hint"] == "XAZU1EG-1SBVA484I"
    assert u1["package_hint"] == "BGA-484"
    assert u1["manufacturer_hint"] == "Xilinx"
    assert u1["source_ref"]["file"].endswith("bom.md")


def test_pinout_table_associates_with_nearest_connector_heading(tmp_path: Path) -> None:
    pin_md = _write(
        tmp_path,
        "pins.md",
        "# Pinouts\n"
        "\n"
        "## 1. J2: nRF FFC\n"
        "\n"
        "| Pin | Signal Name | Voltage |\n"
        "|-----|-------------|---------|\n"
        "| 1   | VDD_IO      | 3.3V    |\n"
        "| 2   | UART_TX     | 3.3V    |\n"
        "\n"
        "## 2. J3: LoRa FFC\n"
        "\n"
        "| Pin | Signal Name | Voltage |\n"
        "|-----|-------------|---------|\n"
        "| 1   | VDD_IO      | 3.3V    |\n"
        "| 4   | SPI_CLK     | 3.3V    |\n",
    )
    artifact = s0.extract([pin_md])
    schema.validate("design_artifact", artifact)
    conns = {c["local_id"]: c for c in artifact["connectors"]}
    assert set(conns.keys()) == {"J2", "J3"}
    assert conns["J2"]["pin_count"] == 2
    assert conns["J2"]["pins"][1]["signal"] == "UART_TX"
    assert conns["J3"]["pins"][1]["signal"] == "SPI_CLK"


def test_run_writes_schema_valid_artifact(tmp_path: Path) -> None:
    md = _write(
        tmp_path,
        "bom.md",
        "| Ref | Part Number | Description | Package |\n"
        "|-----|-------------|-------------|---------|\n"
        "| U1  | AAA | Thing | SOT-23 |\n",
    )
    out = tmp_path / "design_artifact.json"
    artifact = s0.run([md], out)
    assert out.exists()
    # And written file is also valid.
    import json
    loaded = json.loads(out.read_text())
    schema.validate("design_artifact", loaded)
    assert artifact["components"][0]["local_id"] == "U1"


# ---------------------------------------------------------------------------
# Pinout anchoring — the silent data-loss path
#
# Stage 0 skips what it does not recognize and says nothing, so every case below
# used to produce a clean-looking run with pins missing from the board. These
# tests pin down both halves of the contract: which headings anchor, and that
# anything unanchored is preserved separately *and* reported.
# ---------------------------------------------------------------------------


import pytest


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("## J_USB_C Pinout", "J_USB_C"),
        ("## J2: nRF FFC", "J2"),
        ("## 3.1 J_HALOW (u.FL)", "J_HALOW"),
        # The two forms SKILL.md documents, which the original position-anchored
        # regex rejected — a user following the docs exactly lost their table.
        ("## Connector J_USB_C — USB-C receptacle (24-pin)", "J_USB_C"),
        ("## Connector J1 - barrel", "J1"),
        # A pinout for an IC, not a connector. The old pattern required a J.
        ("## U_GNSS (LC76G-PA) pinout", "U_GNSS"),
        # Prose must not be mistaken for a refdes.
        ("## BOM — Core Subsystem", None),
        ("## Power input pinout", None),
        ("## USB signals overview", None),
    ],
)
def test_refdes_in_heading(heading: str, expected: str | None) -> None:
    assert s0.refdes_in_heading(heading) == expected


def _two_unanchored_pinouts(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        "design.md",
        "# Board\n\n"
        "## Power input pinout\n\n"
        "| Pin | Signal |\n|-----|--------|\n| 1 | VBUS |\n| 2 | GND |\n\n"
        "## Audio jack pinout\n\n"
        "| Pin | Signal |\n|-----|--------|\n| 1 | TIP |\n| 2 | RING |\n",
    )


def test_unanchored_pinouts_do_not_merge_into_one_connector(tmp_path: Path) -> None:
    """Two unrelated tables both landing on the id "UNKNOWN" fused a power header
    and an audio jack into one bogus 4-pin part that reached the board."""
    artifact = s0.extract([_two_unanchored_pinouts(tmp_path)])
    by_id = {c["local_id"]: [p["signal"] for p in c["pins"]] for c in artifact["connectors"]}
    assert by_id == {"UNKNOWN_1": ["VBUS", "GND"], "UNKNOWN_2": ["TIP", "RING"]}


def test_unanchored_pinouts_are_reported(tmp_path: Path) -> None:
    artifact = s0.extract([_two_unanchored_pinouts(tmp_path)])
    warnings = artifact["warnings"]
    assert [w["code"] for w in warnings] == ["STAGE0-001", "STAGE0-001"]
    assert "Power input pinout" in warnings[0]["summary"]
    assert "Audio jack pinout" in warnings[1]["summary"]
    # A warning without a location is not actionable.
    assert warnings[0]["source_ref"]["line_start"] > 0


def test_anchored_pinouts_produce_no_warnings(tmp_path: Path) -> None:
    md = _write(
        tmp_path,
        "design.md",
        "# Board\n\n## Connector J_USB_C — USB-C receptacle\n\n"
        "| Pin | Signal |\n|-----|--------|\n| 1 | VBUS |\n| 2 | GND |\n",
    )
    artifact = s0.extract([md])
    assert "warnings" not in artifact
    assert [c["local_id"] for c in artifact["connectors"]] == ["J_USB_C"]


def test_rows_missing_a_pin_or_signal_are_counted_not_swallowed(tmp_path: Path) -> None:
    md = _write(
        tmp_path,
        "design.md",
        "# Board\n\n## J5 header\n\n"
        "| Pin | Signal |\n|-----|--------|\n| 1 | VBUS |\n| 2 |  |\n|  | GND |\n",
    )
    artifact = s0.extract([md])
    skipped = [w for w in artifact["warnings"] if w["code"] == "STAGE0-002"]
    assert len(skipped) == 1
    assert "2 pinout row(s) skipped" in skipped[0]["summary"]
    assert skipped[0]["local_id"] == "J5"


def test_warnings_survive_schema_validation(tmp_path: Path) -> None:
    """`warnings` is a new top-level key and the schema is additionalProperties:false."""
    artifact = s0.run([_two_unanchored_pinouts(tmp_path)], tmp_path / "out.json")
    assert artifact["warnings"]
    schema.validate("design_artifact", artifact)


def test_tables_matching_neither_shape_are_reported(tmp_path: Path) -> None:
    """On a real design this silently swallowed the net-classes table and a GPIO
    map — design intent the user wrote and the pipeline ignored without a word."""
    md = _write(
        tmp_path,
        "overview.md",
        "# Overview\n\n## Net classes\n\n"
        "| Class | Trace width (mm) | Clearance (mm) |\n"
        "|-------|------------------|----------------|\n"
        "| Power | 0.5 | 0.2 |\n",
    )
    artifact = s0.extract([md])
    ignored = [w for w in artifact["warnings"] if w["code"] == "STAGE0-004"]
    assert len(ignored) == 1
    # The columns are what make it triageable without opening the file.
    assert "Trace width (mm)" in ignored[0]["summary"]
    assert ignored[0]["source_ref"]["file"].endswith("overview.md")


def test_a_clean_design_produces_no_warnings_at_all(tmp_path: Path) -> None:
    """Guards against the reporting becoming noise that everyone learns to ignore."""
    md = _write(
        tmp_path,
        "design.md",
        "# Board\n\n## BOM\n\n"
        "| Ref | Description | MPN |\n|-----|-------------|-----|\n| U1 | MCU | STM32 |\n\n"
        "## Connector J1 — power\n\n"
        "| Pin | Signal |\n|-----|--------|\n| 1 | VBUS |\n| 2 | GND |\n",
    )
    artifact = s0.extract([md])
    assert "warnings" not in artifact
