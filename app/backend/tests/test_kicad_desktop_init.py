"""The KiCad desktop's library registration (app/kicad-desktop-init).

Only the stock symbol table here: which stock libraries are enabled, and so
parsed, when a project opens. Run against a scratch tree, with bash.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "kicad-desktop-init" / "50-register-blpl-libraries.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _tree(tmp_path: Path) -> dict[str, Path]:
    stock = tmp_path / "stock"
    for lib in ("Device", "power", "Connector", "Connector_Generic", "Transistor_FET",
                "Amplifier_Audio", "MCU_ST_STM32U5"):
        (stock / f"{lib}.kicad_symdir").mkdir(parents=True)
    projects = tmp_path / "projects"
    board = projects / "board"
    (board / "libraries").mkdir(parents=True)
    (board / "main.kicad_sch").write_text(
        '(kicad_sch (symbol (lib_id "Transistor_FET:Q_NMOS") (at 0 0 0))\n'
        '  (symbol (lib_id "board:MINE") (at 0 0 0)))\n')
    # A schematic inside .git is not one anyone opens.
    (board / ".git").mkdir()
    (board / ".git" / "old.kicad_sch").write_text('(symbol (lib_id "MCU_ST_STM32U5:X"))')
    config = tmp_path / "config"
    return {"stock": stock, "projects": projects, "config": config, "shared": tmp_path / "shared"}


def _run(paths: dict[str, Path], **env: str) -> Path:
    subprocess.run(
        ["bash", str(SCRIPT)], check=True, capture_output=True, text=True,
        env={**os.environ, "BLPL_KICAD_CONFIG_DIR": str(paths["config"]),
             "BLPL_KICAD_PROJECTS_DIR": str(paths["projects"]),
             "BLPL_KICAD_SHARED_DIR": str(paths["shared"]),
             "BLPL_KICAD_STOCK_SYMBOLS_DIR": str(paths["stock"]), **env},
    )
    return paths["config"] / "10.0" / "blpl-stock-sym-lib-table"


def _rows(table: Path) -> dict[str, bool]:
    """name -> enabled, for every row in the table."""
    out = {}
    for line in table.read_text().splitlines():
        m = re.search(r'\(lib \(name "([^"]+)"\)', line)
        if m:
            out[m.group(1)] = "(disabled)" not in line
    return out


def test_only_the_stock_libraries_in_use_are_loaded(tmp_path):
    """Every enabled library is parsed when a project opens, which took a
    minute on a fresh desktop for libraries the board never reads."""
    paths = _tree(tmp_path)
    rows = _rows(_run(paths))
    # Every library is registered, so enabling one is a tick, not a path.
    assert set(rows) == {"Device", "power", "Connector", "Connector_Generic", "Transistor_FET",
                         "Amplifier_Audio", "MCU_ST_STM32U5"}
    assert {n for n, on in rows.items() if on} == {
        "Device", "power", "Connector", "Connector_Generic", "Transistor_FET"}
    # The global table includes it.
    sym = (paths["config"] / "10.0" / "sym-lib-table").read_text()
    assert "blpl-stock-sym-lib-table" in sym


def test_a_library_someone_enabled_stays_enabled_after_a_restart(tmp_path):
    paths = _tree(tmp_path)
    table = _run(paths)
    # Ticked on in Manage Symbol Libraries.
    table.write_text("\n".join(
        line.replace(" (disabled)", "") if '(name "Amplifier_Audio")' in line else line
        for line in table.read_text().splitlines()) + "\n")
    assert _rows(table)["Amplifier_Audio"] is True
    assert _rows(_run(paths))["Amplifier_Audio"] is True


def test_a_table_from_before_this_change_does_not_keep_everything_on(tmp_path):
    """The old table enabled every library; carrying that over would be no change."""
    paths = _tree(tmp_path)
    table = _run(paths, BLPL_KICAD_STOCK_SYMBOLS="all")
    assert all(_rows(table).values())
    assert not _rows(_run(paths))["MCU_ST_STM32U5"]


def test_all_can_still_be_asked_for(tmp_path):
    assert all(_rows(_run(_tree(tmp_path), BLPL_KICAD_STOCK_SYMBOLS="all")).values())


def _pcm_table(paths: dict[str, Path]) -> Path:
    table = paths["config"] / "10.0" / "sym-lib-table"
    table.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f'\t(lib (name "{n}") (type "KiCad") (uri "${{KICAD10_3RD_PARTY}}/symbols/x/{n}.kicad_sym")'
        f' (options "") (descr "Added by Plugin and Content Manager"))\n'
        for n in ("PCM_Unused", "PCM_Used"))
    table.write_text(f"(sym_lib_table\n\t(version 7)\n{rows})\n")
    sch = paths["projects"] / "board" / "main.kicad_sch"
    sch.write_text(sch.read_text() + '(symbol (lib_id "PCM_Used:U1"))\n')
    return table


def test_plugin_manager_libraries_no_schematic_uses_are_switched_off_once(tmp_path):
    paths = _tree(tmp_path)
    table = _pcm_table(paths)
    _run(paths)
    assert _rows(table) == {"PCM_Unused": False, "PCM_Used": True, "board": True, "KiCad": True}
    # Ticked back on by someone: left on at the next start.
    table.write_text(table.read_text().replace(
        '(descr "Added by Plugin and Content Manager") (disabled))',
        '(descr "Added by Plugin and Content Manager"))'))
    _run(paths)
    assert _rows(table)["PCM_Unused"] is True
