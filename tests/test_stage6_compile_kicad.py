"""Smoke tests for pipeline.stage6_compile_kicad — end-to-end from HDM to KiCad outputs."""

from __future__ import annotations

from pathlib import Path

import yaml

from blpl.core import stage6_compile_kicad as s6


def _minimal_hdm(name: str = "Smoke Test") -> dict:
    return {
        "project": {
            "name": name,
            "board_id": "SMK-1",
            "dimensions": [50, 40],
            "stackup": {"layers": 2, "thickness": 1.6, "finish": "HASL"},
        },
        "net_classes": {
            "Default": {"trace_width": 0.2, "clearance": 0.15, "via_dia": 0.6, "via_drill": 0.3}
        },
        "boundaries": {
            "board_outline": {
                "type": "rect",
                "start": [0, 0],
                "end": [50, 40],
                "layer": "Edge.Cuts",
                "width": 0.1,
            }
        },
        "components": {
            "J1": {
                "value": "PJ-063AH",
                "footprint": "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
                "lib_symbol": "Connector:Barrel_Jack",
                "placement": {"x": 10, "y": 10, "rot": 0, "side": "top"},
            }
        },
        "nets": {
            "VIN": {"class": "Default", "pads": [["J1", "1"]]},
            "GND": {"class": "Default", "pads": [["J1", "2"]]},
        },
    }


def test_sanitize_filename_handles_spaces_and_specials() -> None:
    assert s6._sanitize_filename("Example Base Station") == "Example_Base_Station"
    assert s6._sanitize_filename("foo/bar?baz") == "foo_bar_baz"
    assert s6._sanitize_filename("") == "project"


def test_run_produces_sch_pcb_and_pro_files(tmp_path: Path) -> None:
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out_dir = tmp_path / "out"
    results = s6.run(hdm_path, out_dir, stamp="2026-04-20_120000Z")
    sch = results["sch"]
    pcb = results["pcb"]
    pro = results["pro"]
    assert sch.exists() and sch.stat().st_size > 0
    assert pcb.exists() and pcb.stat().st_size > 0
    assert pro.exists() and pro.stat().st_size > 0
    assert sch.read_text().startswith("(kicad_sch")
    assert pcb.read_text().startswith("(kicad_pcb")
    import json
    assert "net_settings" in json.loads(pro.read_text())


def test_run_filenames_carry_shared_timestamp(tmp_path: Path) -> None:
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out_dir = tmp_path / "out"
    stamp = "2026-04-20_120000Z"
    results = s6.run(hdm_path, out_dir, stamp=stamp)
    # All three files share a single basename so KiCad treats them as one project.
    assert results["base"] == f"Smoke_Test_{stamp}"
    assert results["sch"].name == f"Smoke_Test_{stamp}.kicad_sch"
    assert results["pcb"].name == f"Smoke_Test_{stamp}.kicad_pcb"
    assert results["pro"].name == f"Smoke_Test_{stamp}.kicad_pro"


def test_run_auto_generates_stamp_when_omitted(tmp_path: Path) -> None:
    import re as _re
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    results = s6.run(hdm_path, tmp_path / "out")
    # Auto-generated stamp matches YYYY-MM-DD_HHMMSSZ.
    assert _re.search(r"_\d{4}-\d{2}-\d{2}_\d{6}Z$", results["base"])


def test_run_writes_both_library_tables(tmp_path: Path) -> None:
    """The bug: nothing told KiCad where 'Connector' or 'Capacitor_SMD' lived.

    ERC then reported "The current configuration does not include the symbol
    library 'X'" once per reference — 533 of 762 violations across the seven
    example-handheld boards, 70% of the total — and buried the findings that mattered.
    """
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out = tmp_path / "out"

    s6.run(hdm_path, out, stamp="2026-04-20_120000Z")

    sym_table = (out / "sym-lib-table").read_text()
    fp_table = (out / "fp-lib-table").read_text()
    assert sym_table.startswith("(sym_lib_table")
    assert fp_table.startswith("(fp_lib_table")
    assert '(name "Connector")' in sym_table
    assert '(name "Connector_BarrelJack")' in fp_table
    # The URI has to be a path that exists, or the table is decoration.
    for line in sym_table.splitlines():
        if "(uri " in line:
            uri = line.split('(uri "', 1)[1].split('"', 1)[0]
            assert Path(uri).exists(), uri


def test_the_power_library_is_listed_even_though_no_component_names_it(tmp_path: Path) -> None:
    """PWR_FLAG is placed by the schematic emitter, not by any component's
    lib_symbol, so a table built only from components would miss exactly one
    library — and leave behind the ERC noise it was written to remove."""
    hdm_path = tmp_path / "hdm.yaml"
    hdm_path.write_text(yaml.safe_dump(_minimal_hdm()))
    out = tmp_path / "out"

    s6.run(hdm_path, out, stamp="2026-04-20_120000Z")

    assert '(name "power")' in (out / "sym-lib-table").read_text()


def test_a_project_library_shadows_a_stock_one_of_the_same_name(tmp_path: Path) -> None:
    """The table has to agree with what the emitters actually loaded. Roots are
    ordered custom-first, so the first root holding the nickname wins here too."""
    from blpl.emitter import lib_tables

    local = tmp_path / "local"
    local.mkdir()
    (local / "Device.kicad_sym").write_text('(kicad_symbol_lib (symbol "R"))')
    stock = tmp_path / "stock"
    stock.mkdir()
    (stock / "Device.kicad_sym").write_text('(kicad_symbol_lib (symbol "R"))')
    out = tmp_path / "out"
    out.mkdir()

    lib_tables.write(
        {"components": {"R1": {"lib_symbol": "Device:R"}}},
        out,
        symbols_root=[local, stock],
        footprints_root=[tmp_path],
    )

    table = (out / "sym-lib-table").read_text()
    assert str((local / "Device.kicad_sym").resolve()) in table
    assert str((stock / "Device.kicad_sym").resolve()) not in table


def test_a_partially_shadowing_library_does_not_win_the_table(tmp_path: Path) -> None:
    """The bug this module was rewritten for.

    ``loaders`` resolves per *item*, so it takes LGA-8 from stock even though a
    vendored library of the same nickname supplies a different part. A lib
    table maps a nickname to one directory, so picking the first root that
    merely *has* the nickname pointed KiCad at a library holding none of what
    the design used — 'LGA-8_8x6mm_P1.27mm not found in library Package_LGA'
    on a board whose stock library contains it.
    """
    from blpl.emitter import lib_tables

    vendor = tmp_path / "vendor"
    (vendor / "Package_LGA.pretty").mkdir(parents=True)
    (vendor / "Package_LGA.pretty" / "Nordic_LGA-113.kicad_mod").write_text("(footprint)")
    stock = tmp_path / "stock"
    (stock / "Package_LGA.pretty").mkdir(parents=True)
    (stock / "Package_LGA.pretty" / "LGA-8.kicad_mod").write_text("(footprint)")
    out = tmp_path / "out"
    out.mkdir()

    hdm = {"components": {"U1": {"footprint": "Package_LGA:LGA-8"}}}
    lib_tables.write(hdm, out, symbols_root=[tmp_path], footprints_root=[vendor, stock])

    table = (out / "fp-lib-table").read_text()
    assert str((stock / "Package_LGA.pretty").resolve()) in table
    assert str((vendor / "Package_LGA.pretty").resolve()) not in table


def test_a_nickname_split_across_roots_is_merged_not_guessed(tmp_path: Path) -> None:
    """When the design uses one nickname from two roots, no single directory
    can serve it and a table cannot say so. The referenced items are collected
    into a merged library instead, which is the only arrangement that agrees
    with what the emitters actually loaded."""
    from blpl.emitter import lib_tables

    vendor = tmp_path / "vendor"
    (vendor / "Package_SON.pretty").mkdir(parents=True)
    (vendor / "Package_SON.pretty" / "FromVendor.kicad_mod").write_text("(footprint vendor)")
    stock = tmp_path / "stock"
    (stock / "Package_SON.pretty").mkdir(parents=True)
    (stock / "Package_SON.pretty" / "FromStock.kicad_mod").write_text("(footprint stock)")
    out = tmp_path / "out"
    out.mkdir()

    hdm = {"components": {
        "U1": {"footprint": "Package_SON:FromVendor"},
        "U2": {"footprint": "Package_SON:FromStock"},
    }}
    report = lib_tables.write(hdm, out, symbols_root=[tmp_path], footprints_root=[vendor, stock])

    merged = out / lib_tables.MERGE_DIRNAME / "Package_SON.pretty"
    assert (merged / "FromVendor.kicad_mod").read_text() == "(footprint vendor)"
    assert (merged / "FromStock.kicad_mod").read_text() == "(footprint stock)"
    assert str(merged.resolve()) in (out / "fp-lib-table").read_text()
    assert report["merged"] == [{
        "kind": "footprint", "lib": "Package_SON", "merged": True,
        "items": ["FromStock", "FromVendor"],
        "sources": sorted({str(stock / "Package_SON.pretty"), str(vendor / "Package_SON.pretty")}),
    }]


def test_an_item_no_root_provides_is_named_not_just_its_library(tmp_path: Path) -> None:
    from blpl.emitter import lib_tables

    out = tmp_path / "out"
    out.mkdir()
    hdm = {"components": {"U1": {"lib_symbol": "Nope:Thing", "footprint": "NopeFP:Thing"}}}

    report = lib_tables.write(hdm, out, symbols_root=[tmp_path], footprints_root=[tmp_path])

    assert report["unresolved"] == ["symbol:Nope:Thing", "footprint:NopeFP:Thing"]
    assert "Nope" not in (out / "sym-lib-table").read_text()
