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
