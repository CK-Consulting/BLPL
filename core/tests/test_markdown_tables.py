"""Unit tests for pipeline.markdown_tables."""

from __future__ import annotations

from blpl.core import markdown_tables as mdt


def _sample_doc() -> str:
    return (
        "# Title\n"
        "\n"
        "Some intro prose.\n"
        "\n"
        "## BOM\n"
        "\n"
        "| Ref | Part Number | Description | Package |\n"
        "|-----|-------------|-------------|---------|\n"
        "| U1  | **XAZU1EG-1SBVA484I** | Zynq UltraScale+ MPSoC | BGA-484 |\n"
        "| U2  | TPS65086RSMR | Power PMIC | QFN-48 |\n"
        "\n"
        "## 1. J2: nRF FFC pinout\n"
        "\n"
        "| Pin | Signal Name | Voltage | Function |\n"
        "|-----|-------------|---------|----------|\n"
        "| 1   | VDD_IO      | 3.3V    | Power    |\n"
        "| 2   | UART_TX     | 3.3V    | Output   |\n"
    )


def test_extracts_both_tables_with_line_refs() -> None:
    tables = mdt.extract_tables(_sample_doc(), source_file="sample.md")
    assert len(tables) == 2

    bom = tables[0]
    assert bom.headers == ["Ref", "Part Number", "Description", "Package"]
    assert bom.rows[0]["Ref"] == "U1"
    # Stripped of bold markers.
    assert bom.rows[0]["Part Number"] == "XAZU1EG-1SBVA484I"
    # Line refs are 1-based and sensible.
    assert bom.line_start >= 1 and bom.line_end >= bom.line_start

    pinout = tables[1]
    assert "Pin" in pinout.headers
    assert pinout.rows[0]["Pin"] == "1"
    assert pinout.rows[0]["Signal Name"] == "VDD_IO"


def test_classify_bom_vs_pinout_vs_other() -> None:
    bom, pinout = mdt.extract_tables(_sample_doc(), "s.md")
    assert mdt.classify(bom) == "bom"
    assert mdt.classify(pinout) == "pinout"

    other_doc = (
        "| Foo | Bar |\n"
        "|-----|-----|\n"
        "| 1   | 2   |\n"
    )
    (t,) = mdt.extract_tables(other_doc, "s.md")
    assert mdt.classify(t) == "other"


def test_ragged_rows_are_padded_or_truncated() -> None:
    doc = (
        "| A | B | C |\n"
        "|---|---|---|\n"
        "| 1 | 2 |\n"              # short — padded
        "| 1 | 2 | 3 | 4 |\n"       # long — truncated
    )
    (t,) = mdt.extract_tables(doc, "s.md")
    assert t.rows[0] == {"A": "1", "B": "2", "C": ""}
    assert t.rows[1] == {"A": "1", "B": "2", "C": "3"}
