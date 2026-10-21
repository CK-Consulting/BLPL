"""Importing an existing board, and lifting a reusable block out of it.

The payoff being tested is the carrier-board workflow: a proven charging section
on one board becomes a module, and a new design names it and gets its parts.
What matters most is the *boundary* — which nets become ports — because that is
the module's contract, and a wrong boundary produces a module that looks fine
and cannot be wired up.
"""

from __future__ import annotations

import json
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


# -- the gaps the first trial extraction found --------------------------------


def test_a_module_records_every_parts_pins_on_its_nets(board_dir) -> None:
    """The wiring a carrier needs. Without it a module arrived as loose parts."""
    spec = plan_module(read_project(board_dir), ["U1", "C1"], name="charger")
    bom = {row["refdes"]: row for row in spec.bom}
    assert bom["U1"]["pins"] == {"1": "VBAT", "2": "SDA", "3": "CHG_INT"}
    assert bom["C1"]["pins"] == {"1": "VBAT", "2": "CHG_INT"}


def test_a_pad_kicad_calls_unconnected_is_nc_not_a_private_net(tmp_path) -> None:
    d = tmp_path / "b"
    d.mkdir()
    (d / "b.kicad_pcb").write_text(
        '(kicad_pcb (footprint "X:Y" (property "Reference" "U1")'
        ' (pad "1" smd rect (net 1 "VDD")) (pad "2" smd rect (net 2 "unconnected-(U1-NC-Pad2)")))'
        ' (footprint "X:Z" (property "Reference" "U2") (pad "1" smd rect (net 1 "VDD"))))',
        encoding="utf-8",
    )
    spec = plan_module(read_project(d), ["U1"], name="m")
    assert spec.bom[0]["pins"] == {"1": "VDD", "2": "NC"}
    assert spec.internal_nets == [] and [p.name for p in spec.ports] == ["VDD"]


def _carrier_with(module_project_board: Path, tmp_path: Path, refs: list[str], md_extra: str = "") -> dict:
    project = tmp_path / "carrier"
    project.mkdir()
    write_module(plan_module(read_project(module_project_board), refs, name="chg"), project)
    (project / "design.md").write_text(
        "# Carrier\n\n## Modules\n\n- chg\n\n" + md_extra, encoding="utf-8"
    )
    return stage0_deterministic.extract([project / "design.md"])


def test_naming_a_module_brings_its_wiring_ports_by_name_internals_prefixed(board_dir, tmp_path) -> None:
    artifact = _carrier_with(
        board_dir, tmp_path, ["U1", "C1"],
        "## BOM\n\n| Ref | Description |\n| --- | --- |\n| R1 | pull-up |\n\n"
        "## Connections\n\n| Ref | Pin 1 | Pin 2 |\n| --- | --- | --- |\n| R1 | SDA | VBAT |\n",
    )
    from blpl.core import schema

    schema.validate("design_artifact", artifact)
    pins = {c["local_id"]: {p["pin"]: p["signal"] for p in c["pins"]} for c in artifact["connectors"]}
    # Ports keep their names, which is how the carrier's R1 joins them.
    assert pins["chg.U1"] == {"1": "VBAT", "2": "SDA", "3": "chg.CHG_INT"}
    assert pins["chg.C1"] == {"1": "VBAT", "2": "chg.CHG_INT"}
    assert pins["R1"] == {"1": "SDA", "2": "VBAT"}


def test_two_modules_internal_nets_never_merge(board_dir, tmp_path) -> None:
    board = read_project(board_dir)
    project = tmp_path / "carrier"
    project.mkdir()
    for name in ("a", "b"):
        write_module(plan_module(board, ["U1", "C1"], name=name), project)
    (project / "design.md").write_text("# C\n\n## Modules\n\n- a\n- b\n", encoding="utf-8")
    artifact = stage0_deterministic.extract([project / "design.md"])
    signals = {p["signal"] for c in artifact["connectors"] for p in c["pins"]}
    assert {"a.CHG_INT", "b.CHG_INT", "VBAT", "SDA"} <= signals


def test_a_module_extracted_before_pins_were_recorded_says_its_parts_are_unwired(board_dir, tmp_path) -> None:
    project = tmp_path / "carrier"
    project.mkdir()
    path, _ = write_module(plan_module(read_project(board_dir), ["U1"], name="old"), project)
    rows = json.loads((path / "bom.json").read_text())
    for row in rows:
        row.pop("pins", None)
    (path / "bom.json").write_text(json.dumps(rows))
    (project / "design.md").write_text("# C\n\n## Modules\n\n- old\n", encoding="utf-8")
    artifact = stage0_deterministic.extract([project / "design.md"])
    assert any(w["code"] == "STAGE0-009" and "U1" in w["summary"] for w in artifact["warnings"])
    assert not artifact["connectors"]


def test_footprints_come_from_the_boards_own_library_table(board_dir, tmp_path) -> None:
    """The stock root alone missed every project and vendor footprint: on the
    ear tag, the battery contacts, the backup cap, the GNSS and LoRa modules."""
    libs = board_dir / "libs" / "Package_QFN.pretty"
    libs.mkdir(parents=True)
    (libs / "QFN-28.kicad_mod").write_text('(footprint "QFN-28")', encoding="utf-8")
    (board_dir / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Package_QFN") (type "KiCad")'
        ' (uri "${KIPRJMOD}/libs/Package_QFN.pretty") (options "") (descr "")))',
        encoding="utf-8",
    )
    project = tmp_path / "p"
    project.mkdir()
    path, notes = write_module(plan_module(read_project(board_dir), ["U1"], name="m"), project)
    assert (path / "Package_QFN.pretty" / "QFN-28.kicad_mod").is_file()
    assert not any("QFN-28" in n for n in notes)


def test_footprint_roots_are_searched_in_order(board_dir, tmp_path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for root, body in ((first, "first"), (second, "second")):
        (root / "Package_QFN.pretty").mkdir(parents=True)
        (root / "Package_QFN.pretty" / "QFN-28.kicad_mod").write_text(body, encoding="utf-8")
    project = tmp_path / "p"
    project.mkdir()
    path, _ = write_module(plan_module(read_project(board_dir), ["U1"], name="m"), project,
                           footprint_roots=[first, second])
    assert (path / "Package_QFN.pretty" / "QFN-28.kicad_mod").read_text() == "first"


def test_hidden_directories_are_not_part_of_the_design(board_dir) -> None:
    """A BLPL project keeps every earlier emit under .pipeline/; reading those
    merged past revisions of the board into one."""
    old = board_dir / ".pipeline"
    old.mkdir()
    (old / "old.kicad_pcb").write_text(
        '(kicad_pcb (footprint "X:Y" (property "Reference" "U9") (pad "1" smd rect (net 1 "OLD"))))',
        encoding="utf-8",
    )
    board = read_project(board_dir)
    assert "U9" not in board.components and "OLD" not in board.nets


def test_a_board_is_named_after_its_project_file(tmp_path) -> None:
    d = tmp_path / "kicad"
    d.mkdir()
    (d / "ear-tag.kicad_pro").write_text("{}", encoding="utf-8")
    (d / "ear-tag.kicad_pcb").write_text(_PCB, encoding="utf-8")
    assert read_project(d).name == "ear-tag"


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_provenance_records_the_commit_and_whether_the_tree_was_clean(board_dir) -> None:
    _git(board_dir, "init", "-q")
    _git(board_dir, "add", ".")
    _git(board_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    sha = _git(board_dir, "rev-parse", "HEAD")

    clean = plan_module(read_project(board_dir), ["U1"], name="m").to_manifest()["provenance"]
    assert clean["source_sha"] == sha and clean["source_dirty"] is False

    (board_dir / "dev.03.kicad_pcb").write_text(_PCB + "\n", encoding="utf-8")
    spec = plan_module(read_project(board_dir), ["U1"], name="m")
    assert spec.to_manifest()["provenance"]["source_dirty"] is True
    assert any("uncommitted" in w for w in spec.warnings)


def test_a_board_outside_git_says_no_commit_was_recorded(board_dir) -> None:
    spec = plan_module(read_project(board_dir), ["U1"], name="m")
    assert spec.source_sha == "" and "source_dirty" not in spec.to_manifest()["provenance"]
    assert any("not in a git repository" in w for w in spec.warnings)
