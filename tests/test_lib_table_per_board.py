"""One output directory, several boards, one filename KiCad will read.

KiCad finds a library table by looking for a file named exactly
``sym-lib-table`` beside the ``.kicad_pro``, so the canonical pair cannot be
board-qualified. But every board of a multi-board project compiles into the
same directory, so each board's table overwrote the last one's and opening any
board except the most recently compiled handed KiCad another board's
libraries. ERC never caught it, because ERC runs immediately after the stage 6
that wrote the table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blpl.emitter import lib_tables


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    sym = tmp_path / "sym"
    fp = tmp_path / "fp"
    (sym / "Device.kicad_symdir").mkdir(parents=True)
    (sym / "Device.kicad_symdir" / "R.kicad_sym").write_text("(kicad_symbol_lib)")
    (sym / "Radio.kicad_symdir").mkdir(parents=True)
    (sym / "Radio.kicad_symdir" / "LR2021.kicad_sym").write_text("(kicad_symbol_lib)")
    (fp / "Resistor_SMD.pretty").mkdir(parents=True)
    (fp / "Resistor_SMD.pretty" / "R_0402.kicad_mod").write_text("(footprint)")
    return sym, fp


def _hdm(components: dict) -> dict:
    return {"project": {"name": "T"}, "components": components, "nets": {}}


_R = {"R1": {"lib_symbol": "Device:R", "footprint": "Resistor_SMD:R_0402"}}
_RADIO = {"U1": {"lib_symbol": "Radio:LR2021", "footprint": "Resistor_SMD:R_0402"}}


def test_each_board_gets_its_own_table(tmp_path, roots):
    sym, fp = roots
    out = tmp_path / "out"
    out.mkdir()
    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp, board="core")
    lib_tables.write(_hdm(_RADIO), out, symbols_root=sym, footprints_root=fp, board="sb-lora")

    assert (out / "sym-lib-table.core").is_file()
    assert (out / "sym-lib-table.sb-lora").is_file()
    assert "Device" in (out / "sym-lib-table.core").read_text()
    assert "Radio" in (out / "sym-lib-table.sb-lora").read_text()
    # The per-board record is exactly that board's, not a union.
    assert "Radio" not in (out / "sym-lib-table.core").read_text()


def test_the_canonical_table_serves_every_board_not_just_the_last(tmp_path, roots):
    """This is the bug. Compiling sb-lora last must not strip core's libraries."""
    sym, fp = roots
    out = tmp_path / "out"
    out.mkdir()
    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp, board="core")
    lib_tables.write(_hdm(_RADIO), out, symbols_root=sym, footprints_root=fp, board="sb-lora")

    canonical = (out / "sym-lib-table").read_text()
    assert "Radio" in canonical
    assert "Device" in canonical, "core's libraries were dropped by a later board"


def test_a_single_board_project_writes_only_the_canonical_pair(tmp_path, roots):
    sym, fp = roots
    out = tmp_path / "out"
    out.mkdir()
    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp)

    assert (out / "sym-lib-table").is_file()
    assert not list(out.glob("sym-lib-table.*")), "nothing to collide with, so no suffixed copies"


def test_two_boards_disagreeing_about_a_nickname_is_reported(tmp_path, roots):
    """One table maps a nickname to exactly one directory, so a real
    disagreement cannot be expressed. Say so rather than decide it quietly."""
    sym, fp = roots
    other = tmp_path / "sym2"
    (other / "Device.kicad_symdir").mkdir(parents=True)
    (other / "Device.kicad_symdir" / "R.kicad_sym").write_text("(kicad_symbol_lib)")
    out = tmp_path / "out"
    out.mkdir()

    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp, board="core")
    report = lib_tables.write(_hdm(_R), out, symbols_root=other, footprints_root=fp, board="sb-ble")

    clash = [c for c in report["conflicts"] if c["lib"] == "Device"]
    assert clash, "a nickname resolving to two different roots must be reported"
    assert len(clash[0]["candidates"]) == 2
    # The board being compiled now is the one whose ERC is about to run.
    assert str(other) in clash[0]["chosen"]
    assert str(other) in (out / "sym-lib-table").read_text()


def test_recompiling_one_board_does_not_duplicate_or_drop_entries(tmp_path, roots):
    sym, fp = roots
    out = tmp_path / "out"
    out.mkdir()
    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp, board="core")
    lib_tables.write(_hdm(_RADIO), out, symbols_root=sym, footprints_root=fp, board="sb-lora")
    lib_tables.write(_hdm(_R), out, symbols_root=sym, footprints_root=fp, board="core")

    canonical = (out / "sym-lib-table").read_text()
    assert canonical.count('(name "Device")') == 1
    assert canonical.count('(name "Radio")') == 1


def test_a_nickname_split_across_boards_is_merged_not_dropped(tmp_path):
    """The case that made this necessary on the real project.

    core needs Package_LGA:LGA-8, which only stock has. sb-cell needs
    Package_LGA:Nordic_LGA-113, which only the vendor library has. One table
    maps Package_LGA to one directory, so before merging, whichever board
    compiled last silently took the nickname and the other board's footprint
    became unresolvable.
    """
    stock = tmp_path / "stock"
    vendor = tmp_path / "vendor"
    (stock / "Package_LGA.pretty").mkdir(parents=True)
    (stock / "Package_LGA.pretty" / "LGA-8.kicad_mod").write_text("(footprint)")
    (vendor / "Package_LGA.pretty").mkdir(parents=True)
    (vendor / "Package_LGA.pretty" / "Nordic_LGA-113.kicad_mod").write_text("(footprint)")
    sym = tmp_path / "sym"
    (sym / "Device.kicad_symdir").mkdir(parents=True)
    (sym / "Device.kicad_symdir" / "R.kicad_sym").write_text("(kicad_symbol_lib)")

    out = tmp_path / "out"
    out.mkdir()
    lib_tables.write(
        _hdm({"U1": {"lib_symbol": "Device:R", "footprint": "Package_LGA:LGA-8"}}),
        out, symbols_root=sym, footprints_root=[stock, vendor], board="core")
    report = lib_tables.write(
        _hdm({"U1": {"lib_symbol": "Device:R", "footprint": "Package_LGA:Nordic_LGA-113"}}),
        out, symbols_root=sym, footprints_root=[vendor, stock], board="sb-cell")

    merged_dir = out / lib_tables.MERGE_DIRNAME / "Package_LGA.pretty"
    assert (merged_dir / "LGA-8.kicad_mod").is_file(), "core's footprint was dropped"
    assert (merged_dir / "Nordic_LGA-113.kicad_mod").is_file(), "sb-cell's footprint was dropped"
    assert str(merged_dir.resolve()) in (out / "fp-lib-table").read_text()

    entry = [c for c in report["conflicts"] if c["lib"] == "Package_LGA"]
    assert entry and entry[0]["merged"] is True
    assert sorted(entry[0]["items"]) == ["LGA-8", "Nordic_LGA-113"]

    # Each board's own table still points at the root that board resolved.
    assert str((stock / "Package_LGA.pretty").resolve()) in (out / "fp-lib-table.core").read_text()
    assert str((vendor / "Package_LGA.pretty").resolve()) in (out / "fp-lib-table.sb-cell").read_text()
