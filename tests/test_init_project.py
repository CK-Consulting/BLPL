"""Unit tests for blpl.core.init_project.

The contract: `init` reads config the user *already wrote* in their markdown, and
never passes off an invented value as one of theirs. Every value it could not find
must show up in `defaulted`, so "did I write that 110mm, or did the tool?" always
has an answer.
"""

from __future__ import annotations

from pathlib import Path

from blpl.core import init_project


OVERVIEW = """
# Board — Overview

## Project identity

| Field | Value |
|---|---|
| Name | EXAMPLE-S Unified Baseboard |
| Board ID | BB-UNIFIED-V1 |
| Dimensions | 110mm × 70mm (placeholder — pending enclosure work) |

## Stackup

4-layer, 1.6mm, ENIG. Layer order: Top signal / GND plane / Power / Bottom.

## Net classes

| Class | Trace width (mm) | Clearance (mm) | Via dia (mm) | Via drill (mm) |
|---|---|---|---|---|
| Default | 0.15 | 0.15 | 0.3 | 0.15 |
| RF_50Ohm | 0.3 | 0.3 | 0.3 | 0.15 |
"""


def test_harvests_identity_stackup_and_net_classes(tmp_path: Path) -> None:
    (tmp_path / "overview.md").write_text(OVERVIEW, encoding="utf-8")
    result = init_project.build_config(tmp_path)
    proj = result.config["project"]

    assert proj["name"] == "EXAMPLE-S Unified Baseboard"
    assert proj["board_id"] == "BB-UNIFIED-V1"
    # The "×" and the trailing parenthetical must not defeat the parse.
    assert proj["dimensions"] == [110.0, 70.0]
    assert proj["stackup"] == {"layers": 4, "thickness": 1.6, "finish": "ENIG"}

    assert result.config["net_classes"]["RF_50Ohm"]["trace_width"] == 0.3
    assert result.config["net_classes"]["Default"]["via_drill"] == 0.15

    # Everything came from the markdown; nothing was invented.
    assert result.defaulted == []


def test_board_outline_follows_the_declared_dimensions(tmp_path: Path) -> None:
    (tmp_path / "overview.md").write_text(OVERVIEW, encoding="utf-8")
    outline = init_project.build_config(tmp_path).config["boundaries"]["board_outline"]
    assert outline["end"] == [110.0, 70.0]


def test_missing_values_are_reported_as_defaults_not_passed_off_as_yours(tmp_path: Path) -> None:
    (tmp_path / "overview.md").write_text("# Board\n\nNothing useful here.\n", encoding="utf-8")
    result = init_project.build_config(tmp_path)

    assert result.found == []
    joined = " ".join(result.defaulted)
    assert "dimensions" in joined
    assert "stackup" in joined
    # Stage 4 puts every unmatched net in Default, so it must always exist.
    assert "Default" in result.config["net_classes"]


def test_refuses_to_clobber_an_existing_config(tmp_path: Path) -> None:
    (tmp_path / "overview.md").write_text(OVERVIEW, encoding="utf-8")
    (tmp_path / "project.yaml").write_text("project: {name: mine}\n", encoding="utf-8")

    try:
        init_project.write_config(tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("should not overwrite without --force")

    target, _ = init_project.write_config(tmp_path, force=True)
    assert "EXAMPLE-S" in target.read_text()


def test_underscored_headers_parse_instead_of_silently_defaulting(tmp_path):
    """The header spelling a design doc actually uses. `via_dia` never matched
    the search for "via dia", and the miss was silent: on a real board every
    via dimension quietly became the default — 0.6/0.3 emitted where the table
    said 0.8/0.4 — while trace_width survived only because "width" happens to
    be a substring of it. Board rules are the one place a silent default is
    copper."""
    md = tmp_path / "design.md"
    md.write_text(
        "## Net classes\n\n"
        "| Class | Applies to | trace_width | clearance | via_dia | via_drill |\n"
        "|---|---|---|---|---|---|\n"
        "| Power_Bulk | rails | 0.40 | 0.20 | 0.80 | 0.40 |\n"
    )
    result = init_project.build_config(tmp_path)

    power = result.config["net_classes"]["Power_Bulk"]
    assert power["via_dia"] == 0.8
    assert power["via_drill"] == 0.4
    assert power["trace_width"] == 0.4
    assert not any("Power_Bulk" in d for d in result.defaulted)


def test_a_partially_parsed_class_row_names_its_defaulted_fields(tmp_path):
    """The dangerous case: the class name is recognized so nothing looks
    wrong, while individual fields fall to defaults. Each one is named."""
    md = tmp_path / "design.md"
    md.write_text(
        "## Net classes\n\n"
        "| Class | trace_width |\n|---|---|\n| RF_50Ohm | 0.35 |\n"
    )
    result = init_project.build_config(tmp_path)

    assert result.config["net_classes"]["RF_50Ohm"]["trace_width"] == 0.35
    assert any("RF_50Ohm: via_dia" in d for d in result.defaulted)
    assert any("RF_50Ohm: via_drill" in d for d in result.defaulted)


def test_a_board_config_lands_beside_its_markdown_not_at_the_root(tmp_path: Path) -> None:
    """Multi-board: each board has its own dimensions, so init --board writes
    <board>/project.yaml. The libraries directory stays at the project root —
    two boards that mate must resolve a part identically."""
    board = tmp_path / "sb-ant"
    board.mkdir()
    (board / "overview.md").write_text(OVERVIEW, encoding="utf-8")
    target, _ = init_project.write_config(tmp_path, board="sb-ant", target_dir=board)
    assert target == board / "project.yaml"
    assert target.exists()
    assert not (tmp_path / "project.yaml").exists()
    assert (tmp_path / "libraries" / "symbols").is_dir()


def test_generated_config_carries_the_test_point_policy(tmp_path: Path) -> None:
    (tmp_path / "overview.md").write_text(OVERVIEW, encoding="utf-8")
    target, result = init_project.write_config(tmp_path)
    assert result.config["test_points"]["policy"] == "power"
    text = target.read_text(encoding="utf-8")
    assert "test_points:" in text
    # The choice is explained where the value is, not in a doc nobody opens.
    assert "power  one per power/ground net" in text
