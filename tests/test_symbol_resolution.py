"""Symbol references get validated against real libraries, and placeholders shout.

Stage 1 asks an LLM for a KiCad symbol name and it invents plausible ones that do
not exist. Emitting those produced a schematic with dangling lib_ids that KiCad
could not open and browser viewers hung on.

The fix substitutes a placeholder — which is itself dangerous, because a placeholder
that looks like a real part is how a board gets fabricated with the wrong footprint.
So the tests here are as much about the *loudness* as about the resolution.
"""

from __future__ import annotations

from pathlib import Path

from blpl.core import symbol_resolution as sr


def _stock(tmp_path: Path) -> Path:
    """A minimal stock library holding one real symbol."""
    root = tmp_path / "stock"
    (root / "Device.kicad_symdir").mkdir(parents=True)
    (root / "Device.kicad_symdir" / "R.kicad_sym").write_text('(symbol "R")', encoding="utf-8")
    return root


def test_a_real_symbol_resolves_to_itself(tmp_path: Path) -> None:
    res = sr.resolve("Device:R", project_dir=tmp_path, stock_root=_stock(tmp_path))
    assert res.ref == "Device:R"
    assert res.source == sr.STOCK
    assert not res.needs_manual_symbol


def test_an_invented_symbol_becomes_a_flagged_placeholder(tmp_path: Path) -> None:
    res = sr.resolve(
        "RF_GPS:LC76G", project_dir=tmp_path, stock_root=_stock(tmp_path), pin_count=18
    )
    assert res.needs_manual_symbol
    assert res.source == sr.PLACEHOLDER
    assert res.requested == "RF_GPS:LC76G"
    # Pin count is preserved so nets still attach and the board still routes.
    assert res.ref == "Connector_Generic:Conn_01x18"
    assert "does not exist" in res.reason


def test_hand_authored_symbols_beat_everything_else(tmp_path: Path) -> None:
    """Your own work must win over a guess — and over the stock library."""
    custom = tmp_path / sr.CUSTOM_LIB_DIRNAME / "symbols" / "RF_GPS.kicad_symdir"
    custom.mkdir(parents=True)
    (custom / "LC76G.kicad_sym").write_text('(symbol "LC76G")', encoding="utf-8")

    res = sr.resolve("RF_GPS:LC76G", project_dir=tmp_path, stock_root=_stock(tmp_path))
    assert res.source == sr.CUSTOM
    assert res.ref == "RF_GPS:LC76G"
    assert not res.needs_manual_symbol


def test_search_order_is_custom_then_generated_then_stock(tmp_path: Path) -> None:
    order = [source for _, source in sr.search_path(tmp_path, Path("/stock"))]
    assert order == [sr.CUSTOM, sr.GENERATED, sr.STOCK]


def test_a_missing_symbol_name_still_yields_a_usable_placeholder(tmp_path: Path) -> None:
    res = sr.resolve(None, project_dir=tmp_path, stock_root=_stock(tmp_path), pin_count=4)
    assert res.needs_manual_symbol
    assert res.ref == "Connector_Generic:Conn_01x04"


def test_the_report_says_do_not_fabricate(tmp_path: Path) -> None:
    resolutions = {
        "U_GNSS": sr.Resolution(
            ref="Connector_Generic:Conn_01x18",
            source=sr.PLACEHOLDER,
            requested="RF_GPS:LC76G",
            reason="RF_GPS:LC76G does not exist in any symbol library",
        ),
        "R1": sr.Resolution(ref="Device:R", source=sr.STOCK, requested="Device:R"),
    }
    md = sr.render_manual_symbols_md(resolutions, tmp_path)

    assert "DO NOT" in md.upper()
    assert "U_GNSS" in md and "RF_GPS:LC76G" in md
    assert "R1" not in md, "a real symbol must not be listed as needing manual work"
    assert "libraries" in md, "the report must say where to put the symbol you draw"


def _stock_footprints(tmp_path: Path) -> Path:
    """A minimal stock footprint library holding one real land pattern."""
    root = tmp_path / "fp"
    (root / "Resistor_SMD.pretty").mkdir(parents=True)
    (root / "Resistor_SMD.pretty" / "R_0402_1005Metric.kicad_mod").write_text(
        "(footprint)", encoding="utf-8"
    )
    return root


def test_a_real_footprint_resolves_to_itself(tmp_path: Path) -> None:
    res = sr.resolve_footprint(
        "Resistor_SMD:R_0402_1005Metric",
        project_dir=tmp_path,
        stock_root=_stock_footprints(tmp_path),
    )
    assert res.ref == "Resistor_SMD:R_0402_1005Metric"
    assert res.source == sr.STOCK
    assert not res.needs_manual_symbol


def test_an_invented_footprint_becomes_a_pad_count_correct_placeholder(tmp_path: Path) -> None:
    """The placeholder must have the right pad count or the netlist won't attach —
    and it must be obviously not a QFN, so nobody mistakes it for the real part."""
    res = sr.resolve_footprint(
        "Sensor_Motion:ICM-42670-P_QFN-14",
        project_dir=tmp_path,
        stock_root=_stock_footprints(tmp_path),
        pin_count=14,
    )
    assert res.needs_manual_symbol
    assert res.ref == "Connector_PinHeader_2.54mm:PinHeader_1x14_P2.54mm_Vertical"
    assert res.requested == "Sensor_Motion:ICM-42670-P_QFN-14"


def test_hand_authored_footprints_beat_the_stock_library(tmp_path: Path) -> None:
    lib = tmp_path / sr.CUSTOM_LIB_DIRNAME / "footprints" / "MyParts.pretty"
    lib.mkdir(parents=True)
    (lib / "LC76G.kicad_mod").write_text("(footprint)", encoding="utf-8")

    res = sr.resolve_footprint(
        "MyParts:LC76G", project_dir=tmp_path, stock_root=_stock_footprints(tmp_path)
    )
    assert res.source == sr.CUSTOM
    assert not res.needs_manual_symbol


def test_the_report_leads_with_footprints(tmp_path: Path) -> None:
    """A wrong symbol is a wrong drawing; a wrong footprint is wrong copper."""
    md = sr.render_manual_symbols_md(
        {"U1": sr.Resolution(ref="Device:R", source=sr.STOCK, requested="Device:R")},
        tmp_path,
        {
            "U1": sr.Resolution(
                ref="Connector_PinHeader_2.54mm:PinHeader_1x14_P2.54mm_Vertical",
                source=sr.PLACEHOLDER,
                requested="Sensor_Motion:ICM-42670-P_QFN-14",
                reason="does not exist in any footprint library",
            )
        },
    )
    assert "DO NOT FABRICATE" in md
    assert "Missing footprints" in md
    assert "ICM-42670-P_QFN-14" in md
    assert "Missing symbols" not in md, "U1's symbol resolved — it must not be listed"


def test_report_is_written_even_when_there_is_nothing_wrong(tmp_path: Path) -> None:
    """Its absence must never be mistaken for 'nothing to worry about'."""
    md = sr.render_manual_symbols_md(
        {"R1": sr.Resolution(ref="Device:R", source=sr.STOCK, requested="Device:R")}, tmp_path
    )
    assert "None" in md
