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


_TABLE = "| Pin | Signal Name |\n|-----|-------------|\n| 1   | VDD_IO      |\n"


def test_table_inside_a_fence_is_not_a_table() -> None:
    # A decision log quoting an old pin table in a fence made that table a
    # pinout with no part, which doctor flagged and Stage 0 read.
    for fence in ("```", "~~~", "````", "```markdown"):
        close = fence.rstrip("markdown")
        doc = f"## Old pinout\n\n{fence}\n{_TABLE}{close}\n"
        assert mdt.extract_tables(doc, "s.md") == [], fence


def test_table_after_a_closed_fence_is_read_with_its_own_line() -> None:
    doc = "```\n" + _TABLE + "```\n\n" + _TABLE
    (t,) = mdt.extract_tables(doc, "s.md")
    assert t.line_start == 7
    assert t.rows == [{"Pin": "1", "Signal Name": "VDD_IO"}]


def test_fence_closes_only_on_a_matching_bare_fence() -> None:
    # A shorter run, the other character, or a fence with text after it is
    # content of the block; an unclosed fence runs to the end.
    doc = "````\n```\n~~~~\n```` not a close\n" + _TABLE
    assert mdt.extract_tables(doc, "s.md") == []
    assert mdt.extract_tables("```\n" + _TABLE, "s.md") == []


def test_backtick_info_string_with_a_backtick_is_not_a_fence() -> None:
    doc = "```inline` code\n" + _TABLE
    assert len(mdt.extract_tables(doc, "s.md")) == 1


def test_heading_above_skips_comments_in_a_fence() -> None:
    doc = "## U3 pinout\n\n```bash\n# flash it\n```\n\n" + _TABLE
    (t,) = mdt.extract_tables(doc, "s.md")
    assert mdt.heading_above(doc, t.line_start) == ("## U3 pinout", 1)


def test_fence_opened_in_a_list_item_closes_and_later_tables_are_read() -> None:
    # The opener sits after a list marker and the closer is indented under
    # it; missing the opener made the closer open a fence to the end.
    for opener, closer in (("- ```text", "  ```"), ("1. ~~~", "   ~~~"), ("> ```", "> ```")):
        doc = f"{opener}\n{_TABLE}{closer}\n\n## U3 pinout\n\n{_TABLE}"
        (t,) = mdt.extract_tables(doc, "s.md")
        assert mdt.heading_above(doc, t.line_start)[0] == "## U3 pinout", opener
