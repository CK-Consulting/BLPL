"""Importing an existing board, and lifting a reusable block out of it.

The payoff being tested is the carrier-board workflow: a proven charging section
on one board becomes a module, and a new design names it and gets its parts.
What matters most is the *boundary* — which nets become ports — because that is
the module's contract, and a wrong boundary produces a module that looks fine
and cannot be wired up.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from blpl.core import modules_remix, stage0_deterministic
from blpl.core.symbol_resolution import MODULE, search_path
from blpl.importer_kicad import list_modules, plan_module, read_project, write_module

# A tiny two-block board: a charger (U1 + C1) feeding an MCU (U2). VBAT and
# I2C cross between them; CHG_INT is internal to the charger.
_PCB = """
(kicad_pcb
  (footprint "Package_QFN:QFN-28"
    (at 10 10)
    (property "Reference" "U1")
    (property "Value" "LTC4015")
    (property "MPN" "LTC4015EUHF#PBF")
    (pad "1" smd rect (net 1 "VBAT"))
    (pad "2" smd rect (net 2 "SDA"))
    (pad "3" smd rect (net 3 "CHG_INT"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (at 12 10)
    (property "Reference" "C1")
    (property "Value" "10uF")
    (pad "1" smd rect (net 1 "VBAT"))
    (pad "2" smd rect (net 3 "CHG_INT"))
  )
  (footprint "Package_QFP:LQFP-48"
    (at 30 10)
    (property "Reference" "U2")
    (property "Value" "STM32")
    (pad "5" smd rect (net 1 "VBAT"))
    (pad "6" smd rect (net 2 "SDA"))
  )
)
"""


@pytest.fixture
def board_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dev.03"
    d.mkdir()
    (d / "dev.03.kicad_pcb").write_text(_PCB, encoding="utf-8")
    return d


# -- reading somebody else's board -------------------------------------------


def test_a_board_reads_back_as_components_and_nets(board_dir) -> None:
    board = read_project(board_dir)
    assert set(board.components) == {"U1", "C1", "U2"}
    assert board.components["U1"].mpn == "LTC4015EUHF#PBF"
    assert board.components["U1"].footprint == "Package_QFN:QFN-28"
    assert board.components["U1"].position == (10.0, 10.0)
    assert ("U1", "1") in board.nets["VBAT"].members
    assert ("U2", "5") in board.nets["VBAT"].members


def test_a_directory_with_no_kicad_files_says_so_rather_than_returning_nothing(tmp_path) -> None:
    board = read_project(tmp_path)
    assert board.components == {}
    assert any("no .kicad_sch or .kicad_pcb" in w for w in board.warnings)


def test_a_schematic_only_import_warns_that_modules_cannot_be_cut(tmp_path) -> None:
    """Cutting a module means cutting nets, and a schematic alone has none here
    — better to say that than to offer an extraction that would silently produce
    a module with no interface."""
    d = tmp_path / "sch-only"
    d.mkdir()
    (d / "x.kicad_sch").write_text(
        '(kicad_sch (symbol (lib_id "Device:R") (property "Reference" "R1") '
        '(property "Value" "10k")))',
        encoding="utf-8",
    )
    board = read_project(d)
    assert "R1" in board.components
    assert any("connectivity is unknown" in w for w in board.warnings)


def test_power_flag_pseudo_symbols_are_not_parts(tmp_path) -> None:
    d = tmp_path / "pwr"
    d.mkdir()
    (d / "x.kicad_sch").write_text(
        '(kicad_sch (symbol (lib_id "power:GND") (property "Reference" "#PWR01")) '
        '(symbol (lib_id "Device:C") (property "Reference" "C9")))',
        encoding="utf-8",
    )
    board = read_project(d)
    assert list(board.components) == ["C9"]


# -- the boundary is the point ------------------------------------------------


def test_the_nets_that_leave_the_block_become_its_ports(board_dir) -> None:
    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "C1"], name="ltc4015-charger")

    assert spec.components == ["C1", "U1"]
    assert {p.name for p in spec.ports} == {"VBAT", "SDA"}
    # CHG_INT touches only U1 and C1, so it stays inside — exposing it would
    # invite a carrier board to connect to a private node.
    assert spec.internal_nets == ["CHG_INT"]


def test_a_port_records_what_it_was_wired_to(board_dir) -> None:
    """Not a requirement on the next board — the fastest way to understand what
    a port is *for* when you meet the module a year later."""
    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "C1"], name="charger")
    vbat = next(p for p in spec.ports if p.name == "VBAT")
    assert vbat.external_refs == ["U2"]
    assert sorted(vbat.internal_pins) == ["C1.1", "U1.1"]
    assert vbat.kind == "power"


def test_selecting_the_whole_board_is_flagged_not_silently_accepted(board_dir) -> None:
    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "C1", "U2"], name="everything")
    assert not spec.ports
    assert any("electrically isolated" in w for w in spec.warnings)


def test_naming_a_part_that_is_not_on_the_board_is_reported(board_dir) -> None:
    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "U404"], name="charger")
    assert spec.components == ["U1"]
    assert any("U404" in w for w in spec.warnings)


def test_parts_without_an_mpn_are_flagged_at_extraction_not_at_quote(board_dir) -> None:
    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "C1"], name="charger")
    assert any("no MPN" in w and "C1" in w for w in spec.warnings)


# -- what gets written --------------------------------------------------------


def test_a_written_module_is_self_contained(board_dir, tmp_path) -> None:
    """Copied in, not referenced: a module has to survive the board it came from
    being moved, renamed, or deleted."""
    symbols = tmp_path / "stocklib"
    (symbols / "Package_QFN.pretty").mkdir(parents=True)
    (symbols / "Package_QFN.pretty" / "QFN-28.kicad_mod").write_text(
        '(footprint "QFN-28" (pad "1" smd rect))', encoding="utf-8"
    )

    board = read_project(board_dir)
    spec = plan_module(board, ["U1", "C1"], name="ltc4015-charger", description="charger block")
    dest = tmp_path / "newproj"
    dest.mkdir()
    path, notes = write_module(spec, dest, footprint_root=symbols)

    assert (path / "module.yaml").is_file()
    assert (path / "Package_QFN.pretty" / "QFN-28.kicad_mod").is_file()
    manifest = yaml.safe_load((path / "module.yaml").read_text())
    assert manifest["module"] == "ltc4015-charger"
    assert manifest["provenance"]["source_board"] == "dev.03"
    # Power first, then alphabetical: the rails are what you wire up first.
    assert [p["port"] for p in manifest["interface"]] == ["VBAT", "SDA"]
    assert json.loads((path / "bom.json").read_text())[0]["refdes"] == "C1"
    # The one footprint that could not be found is named, not swallowed.
    assert any("C_0402" in n for n in notes)


def test_footprints_keep_their_source_library_name(board_dir, tmp_path) -> None:
    """The module directory serves directly as a library root, so a lib_id
    copied off the source board must still resolve — which only works if the
    library keeps its name."""
    stock = tmp_path / "stock"
    (stock / "Package_QFN.pretty").mkdir(parents=True)
    (stock / "Package_QFN.pretty" / "QFN-28.kicad_mod").write_text("(footprint)", encoding="utf-8")

    board = read_project(board_dir)
    spec = plan_module(board, ["U1"], name="charger")
    project = tmp_path / "carrier"
    project.mkdir()
    write_module(spec, project, footprint_root=stock)

    roots = [r for r, tier in search_path(project, tmp_path / "nope") if tier == MODULE]
    assert roots == [project / "modules" / "charger"]
    assert (roots[0] / "Package_QFN.pretty" / "QFN-28.kicad_mod").is_file()


def test_overwriting_a_module_is_refused_unless_asked_for(board_dir, tmp_path) -> None:
    board = read_project(board_dir)
    spec = plan_module(board, ["U1"], name="charger")
    project = tmp_path / "p"
    project.mkdir()
    write_module(spec, project)
    with pytest.raises(FileExistsError):
        write_module(spec, project)
    write_module(spec, project, overwrite=True)


def test_a_projects_own_module_wins_over_a_shared_one(board_dir, tmp_path, monkeypatch) -> None:
    board = read_project(board_dir)
    project, shared = tmp_path / "p", tmp_path / "shared"
    project.mkdir()
    shared.mkdir()
    write_module(plan_module(board, ["U1"], name="charger", description="local"), project)
    write_module(plan_module(board, ["U1"], name="charger", description="shared"), shared)

    found = list_modules(project, shared)
    assert [m["name"] for m in found] == ["charger"]
    assert found[0]["description"] == "local"


# -- remixing into a new design ----------------------------------------------


def test_a_modules_section_is_read_off_the_markdown() -> None:
    names = modules_remix.parse_modules_section(
        "# Carrier\n\n## Modules\n\n- ltc4015-charger — from dev.03\n"
        "- `lora-frontend`\n\n## BOM\n\n- not-a-module\n"
    )
    assert names == ["ltc4015-charger", "lora-frontend"]


def test_naming_a_module_pulls_its_parts_into_the_design(board_dir, tmp_path) -> None:
    board = read_project(board_dir)
    project = tmp_path / "carrier"
    project.mkdir()
    write_module(plan_module(board, ["U1", "C1"], name="ltc4015-charger"), project)

    (project / "design.md").write_text(
        "# Carrier\n\n## Modules\n\n- ltc4015-charger — unchanged from dev.03\n\n"
        "## BOM\n\n| Ref | Description |\n| --- | --- |\n| J1 | USB-C connector |\n",
        encoding="utf-8",
    )
    artifact = stage0_deterministic.extract([project / "design.md"])

    assert artifact["modules"] == ["ltc4015-charger"]
    ids = {c["local_id"] for c in artifact["components"]}
    # Prefixed: two modules on one carrier would both bring a U1, and merging
    # them would be a wiring error nobody sees until the board comes back.
    assert "ltc4015-charger.U1" in ids and "ltc4015-charger.C1" in ids
    assert "J1" in ids
    u1 = next(c for c in artifact["components"] if c["local_id"] == "ltc4015-charger.U1")
    assert u1["part_hint"] == "LTC4015EUHF#PBF"


def test_a_module_that_cannot_be_found_is_a_warning_not_a_silent_omission(tmp_path) -> None:
    project = tmp_path / "carrier"
    project.mkdir()
    (project / "design.md").write_text(
        "# Carrier\n\n## Modules\n\n- does-not-exist\n", encoding="utf-8"
    )
    artifact = stage0_deterministic.extract([project / "design.md"])
    assert "modules" not in artifact
    codes = {w["code"] for w in artifact["warnings"]}
    assert "STAGE0-006" in codes


def test_a_design_with_no_modules_section_is_completely_unaffected(tmp_path) -> None:
    project = tmp_path / "plain"
    project.mkdir()
    (project / "design.md").write_text(
        "# Board\n\n## BOM\n\n| Ref | Description |\n| --- | --- |\n| U1 | MCU |\n",
        encoding="utf-8",
    )
    artifact = stage0_deterministic.extract([project / "design.md"])
    assert "modules" not in artifact
    assert [c["local_id"] for c in artifact["components"]] == ["U1"]


# -- the tools a conversation actually calls ----------------------------------


def _tool(name: str):
    from app.agent.registry import module_tools

    return next(s for s in module_tools() if s.name == name)


def _ctx(project: Path):
    from app.agent import ToolContext
    from app.references import FilesystemSandbox, ReferenceManifest

    return ToolContext(
        project_id="p",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="p", workspace_root=project)
        ),
    )


def test_the_assistant_can_read_a_board_then_extract_from_it(board_dir, tmp_path) -> None:
    """The two tools share state deliberately: planning against a board the
    assistant just read is what lets it discuss a boundary and then act on the
    same one, rather than re-reading and possibly getting a different answer."""
    import asyncio

    project = tmp_path / "carrier"
    project.mkdir()
    shutil.copytree(board_dir, project / "src")
    ctx = _ctx(project)

    read = json.loads(asyncio.run(_tool("read_kicad_design").handler(ctx, {"directory": "src"})))
    assert {c["refdes"] for c in read["components"]} == {"U1", "C1", "U2"}

    plan = json.loads(
        asyncio.run(
            _tool("plan_module_extraction").handler(ctx, {"refdes": ["U1", "C1"], "name": "chg"})
        )
    )
    assert {p["port"] for p in plan["interface"]} == {"VBAT", "SDA"}

    out = json.loads(
        asyncio.run(_tool("extract_module").handler(ctx, {"refdes": ["U1", "C1"], "name": "chg"}))
    )
    assert out["ports"] == 2
    assert (project / "modules" / "chg" / "module.yaml").is_file()

    listed = json.loads(asyncio.run(_tool("list_modules").handler(ctx, {})))
    assert [m["name"] for m in listed["modules"]] == ["chg"]


def test_extraction_refuses_a_selection_that_is_not_on_the_board(board_dir, tmp_path) -> None:
    """Writing an empty module would look like success and produce a directory
    that fails only when somebody tries to use it."""
    import asyncio

    from app.agent.toolspec import ToolDenied

    project = tmp_path / "carrier"
    project.mkdir()
    shutil.copytree(board_dir, project / "src")
    ctx = _ctx(project)
    asyncio.run(_tool("read_kicad_design").handler(ctx, {"directory": "src"}))

    with pytest.raises(ToolDenied):
        asyncio.run(_tool("extract_module").handler(ctx, {"refdes": ["U404"], "name": "ghost"}))


def test_a_module_name_cannot_escape_the_modules_directory(board_dir, tmp_path) -> None:
    import asyncio

    from app.agent.toolspec import ToolDenied

    ctx = _ctx(tmp_path)
    with pytest.raises(ToolDenied):
        asyncio.run(_tool("extract_module").handler(ctx, {"refdes": ["U1"], "name": "../escape"}))


def test_reading_outside_the_project_is_refused(tmp_path) -> None:
    import asyncio

    from app.agent.toolspec import ToolDenied

    project = tmp_path / "p"
    project.mkdir()
    with pytest.raises(ToolDenied):
        asyncio.run(_tool("read_kicad_design").handler(_ctx(project), {"directory": "../.."}))


def test_a_symlink_out_of_the_project_does_not_widen_the_read(board_dir, tmp_path) -> None:
    """The check resolves before comparing, so a symlink planted in the project
    cannot be used to read a board the user never imported."""
    import asyncio

    from app.agent.toolspec import ToolDenied

    project = tmp_path / "p"
    project.mkdir()
    (project / "elsewhere").symlink_to(board_dir)
    with pytest.raises(ToolDenied):
        asyncio.run(_tool("read_kicad_design").handler(_ctx(project), {"directory": "elsewhere"}))
