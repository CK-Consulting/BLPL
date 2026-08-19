"""Unit tests for blpl.agent.tools.bom.

The parts that talk to kicad-happy's translator run against the real submodule
script — it is the integration point, and stubbing it would test only that the
stub matches what we assumed. Tests that need it skip cleanly when the submodule
is not checked out. Nothing here touches the network: LCSC resolution is stubbed,
because a test that silently passes when LCSC is down is worse than no test.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from blpl.agent.tools import bom

RELEASE_BOM = """\
Designators,Quantity,Value,Footprint,MPN,Manufacturer
"C1,C2,C5",3,100nF 16V X7R,Capacitor_SMD:C_0402_1005Metric,GRM155R71C104KA88D,Murata
U1,1,ESP32-S3,RF_Module:ESP32-S3-WROOM-1,ESP32-S3-WROOM-1-N16R8,Espressif
J1,1,USB-C,Connector_USB:USB_C_Receptacle,,
"""

POSITIONS = """\
Ref,Val,Package,PosX,PosY,Rot,Side
C1,100nF,C_0402,10.5,20.0,0,top
C2,100nF,C_0402,12.5,20.0,90,top
C5,100nF,C_0402,14.5,20.0,180,bottom
U1,ESP32-S3,MOD,30.0,40.0,0,top
J1,USB-C,USBC,5.0,5.0,270,top
R99,10k,R_0402,50.0,50.0,0,top
"""


def _have_translator() -> bool:
    try:
        return bom.script_path("bom", "translate_bom_pnp.py").is_file()
    except bom.KicadHappyMissing:
        return False


needs_translator = pytest.mark.skipif(
    not _have_translator(), reason="kicad-happy submodule not checked out"
)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    (tmp_path / "board-bom.csv").write_text(RELEASE_BOM, encoding="utf-8")
    (tmp_path / "positions.csv").write_text(POSITIONS, encoding="utf-8")
    return tmp_path


def _rows(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Reading what release.py wrote
# ---------------------------------------------------------------------------


def test_grouped_designators_are_split(board: Path) -> None:
    rows = bom.read_bom_csv(board / "board-bom.csv")
    assert [r.designators for r in rows] == [["C1", "C2", "C5"], ["U1"], ["J1"]]
    assert rows[0].quantity == 3


def test_package_drops_the_library_prefix(board: Path) -> None:
    # Both houses want the package, not the KiCad library it came from.
    rows = bom.read_bom_csv(board / "board-bom.csv")
    assert rows[0].package == "C_0402_1005Metric"
    assert rows[1].package == "ESP32-S3-WROOM-1"


def test_quantity_falls_back_to_the_designator_count(tmp_path: Path) -> None:
    (tmp_path / "b.csv").write_text(
        'Designators,Quantity,Value,Footprint,MPN,Manufacturer\n"R1,R2",,10k,F,M,Mfr\n',
        encoding="utf-8",
    )
    assert bom.read_bom_csv(tmp_path / "b.csv")[0].quantity == 2


# ---------------------------------------------------------------------------
# Mounting type — from the library, never from the name
# ---------------------------------------------------------------------------


def _footprint(root: Path, lib: str, name: str, body: str) -> None:
    d = root / f"{lib}.pretty"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.kicad_mod").write_text(body, encoding="utf-8")


def test_mount_type_is_read_from_the_footprint(tmp_path: Path) -> None:
    _footprint(tmp_path, "Cap", "C_0402", '(footprint "C_0402" (attr smd) )')
    _footprint(tmp_path, "Conn", "PinHeader", '(footprint "PinHeader" (attr through_hole) )')
    assert bom.mount_type("Cap:C_0402", [tmp_path]) == "SMD"
    assert bom.mount_type("Conn:PinHeader", [tmp_path]) == "THT"


def test_a_misleading_library_name_does_not_decide_the_type(tmp_path: Path) -> None:
    """The whole reason this reads the file: a through-hole part filed under a
    library called ..._SMD would be quoted for reflow if the name were trusted."""
    _footprint(tmp_path, "Capacitor_SMD", "C_Radial", '(footprint "C_Radial" (attr through_hole) )')
    assert bom.mount_type("Capacitor_SMD:C_Radial", [tmp_path]) == "THT"


def test_an_unanswerable_footprint_is_blank_not_guessed(tmp_path: Path) -> None:
    # A wrong mounting type is a re-quote at best; blank asks the question.
    _footprint(tmp_path, "Lib", "NoAttr", '(footprint "NoAttr" )')
    assert bom.mount_type("Lib:NoAttr", [tmp_path]) == ""
    assert bom.mount_type("Lib:Absent", [tmp_path]) == ""
    assert bom.mount_type("no-colon", [tmp_path]) == ""


# ---------------------------------------------------------------------------
# PCBWay's BOM — ours to write
# ---------------------------------------------------------------------------


def test_pcbway_bom_uses_the_documented_columns_in_order(board: Path, tmp_path: Path) -> None:
    out = tmp_path / "pcbway.csv"
    bom.write_pcbway_bom(bom.read_bom_csv(board / "board-bom.csv"), out, footprint_roots=[])
    with out.open(newline="", encoding="utf-8") as f:
        assert tuple(next(csv.reader(f))) == bom.PCBWAY_COLUMNS


def test_pcbway_bom_numbers_its_lines_and_keeps_grouping(board: Path, tmp_path: Path) -> None:
    out = tmp_path / "pcbway.csv"
    bom.write_pcbway_bom(bom.read_bom_csv(board / "board-bom.csv"), out, footprint_roots=[])
    rows = _rows(out)
    assert [r["Line#"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["Designator"] == "C1,C2,C5"
    assert rows[0]["Qty"] == "3"


def test_pcbway_flags_parts_it_cannot_source(board: Path, tmp_path: Path) -> None:
    """PCBWay turnkey sources by MPN, so a blank MPN is not a cosmetic gap."""
    f = bom.write_pcbway_bom(
        bom.read_bom_csv(board / "board-bom.csv"), tmp_path / "p.csv", footprint_roots=[]
    )
    assert "no MPN" in f.note
    assert "J1" in f.note


# ---------------------------------------------------------------------------
# LCSC — the column JLCPCB actually orders from
# ---------------------------------------------------------------------------


def _jlc_bom(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["Comment", "Designator", "Footprint", "LCSC Part #", "MPN",
             "Manufacturer", "Quantity", "Notes"]
        )
        w.writerows(rows)
    return path


def test_lcsc_numbers_are_written_into_the_bom(tmp_path: Path) -> None:
    path = _jlc_bom(
        tmp_path / "bom.csv",
        [["100nF", "C1,C2", "F", "", "GRM155", "Murata", "2", ""]],
    )
    filled, unbuildable = bom.fill_lcsc_column(
        path, {"GRM155": bom.LcscPart(mpn="GRM155", lcsc="C71629", stock=100)}
    )
    assert filled == 1
    assert unbuildable == []
    assert _rows(path)[0]["LCSC Part #"] == "C71629"


def test_unresolved_lines_are_named_by_designator(tmp_path: Path) -> None:
    """"Which ones" is what the user acts on — a count sends them hunting."""
    path = _jlc_bom(
        tmp_path / "bom.csv",
        [
            ["100nF", "C1,C2", "F", "", "GRM155", "Murata", "2", ""],
            ["USB-C", "J1", "F", "", "", "", "1", ""],
        ],
    )
    filled, unbuildable = bom.fill_lcsc_column(
        path, {"GRM155": bom.LcscPart(mpn="GRM155", lcsc="C71629", stock=100)}
    )
    assert filled == 1
    assert unbuildable == ["J1"]


def test_a_part_with_no_stock_is_not_orderable() -> None:
    # Resolving to a C-number is not the same as being buildable.
    assert bom.LcscPart(mpn="X", lcsc="C1", stock=0).orderable is False
    assert bom.LcscPart(mpn="X", lcsc="", stock=99).orderable is False
    assert bom.LcscPart(mpn="X", lcsc="C1", stock=99).orderable is True


# ---------------------------------------------------------------------------
# Building a house package
# ---------------------------------------------------------------------------


def test_an_unknown_house_is_refused(board: Path, tmp_path: Path) -> None:
    pkg = bom.build_assembly(board / "board-bom.csv", None, tmp_path, house="oshpark")
    assert pkg.ok is False
    assert "unknown assembly house" in pkg.reason


def test_a_missing_bom_is_reported_not_raised(tmp_path: Path) -> None:
    pkg = bom.build_assembly(tmp_path / "nope.csv", None, tmp_path, house="jlcpcb")
    assert pkg.ok is False
    assert "release package" in pkg.reason


def test_a_missing_placement_file_is_a_warning_not_a_failure(board: Path, tmp_path: Path) -> None:
    pkg = bom.build_assembly(board / "board-bom.csv", None, tmp_path, house="pcbway")
    assert pkg.ok is True
    assert any("no placement file" in w for w in pkg.warnings)


@needs_translator
def test_jlcpcb_without_lcsc_says_the_bom_is_not_buildable(board: Path, tmp_path: Path) -> None:
    """The translator leaves LCSC Part # blank by design. A JLCPCB assembly
    order against a blank column uploads fine and then cannot be built."""
    pkg = bom.build_assembly(
        board / "board-bom.csv", board / "positions.csv", tmp_path, house="jlcpcb"
    )
    assert pkg.ok is True
    assert any("LCSC" in w and "blank" in w for w in pkg.warnings)


@needs_translator
def test_placement_rows_with_no_bom_line_are_dropped_and_reported(
    board: Path, tmp_path: Path
) -> None:
    """R99 is placed but absent from the BOM. Both houses reject an upload whose
    CPL names parts the BOM never mentioned, and say which file is wrong: neither."""
    pkg = bom.build_assembly(
        board / "board-bom.csv", board / "positions.csv", tmp_path, house="jlcpcb"
    )
    cpl = next(f for f in pkg.files if f.name.endswith("cpl"))
    assert cpl.rows == 5
    assert any("absent from the BOM" in w for w in pkg.warnings)


@needs_translator
def test_pcbway_cpl_is_filtered_against_the_source_bom(board: Path, tmp_path: Path) -> None:
    # PCBWay's own BOM columns are unreadable to the translator (it wants Value,
    # PCBWay says Description), so the filter has to use the release BOM.
    pkg = bom.build_assembly(
        board / "board-bom.csv", board / "positions.csv", tmp_path, house="pcbway"
    )
    cpl = next(f for f in pkg.files if f.name.endswith("cpl"))
    assert cpl.rows == 5


@needs_translator
def test_the_two_houses_get_different_boms(board: Path, tmp_path: Path) -> None:
    """They source differently — JLCPCB by LCSC number, PCBWay turnkey by MPN —
    so one file labelled for both would quote the wrong parts."""
    for house in bom.HOUSES:
        bom.build_assembly(
            board / "board-bom.csv", board / "positions.csv", tmp_path, house=house
        )
    jlc = (tmp_path / "jlcpcb" / "bom.csv").read_text(encoding="utf-8").splitlines()[0]
    pcb = (tmp_path / "pcbway" / "bom.csv").read_text(encoding="utf-8").splitlines()[0]
    assert jlc != pcb
    assert "LCSC Part #" in jlc
    assert "Line#" in pcb


@needs_translator
def test_lcsc_fill_reaches_the_written_bom(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        bom,
        "resolve_lcsc",
        lambda mpns, **_kw: {
            "GRM155R71C104KA88D": bom.LcscPart(
                mpn="GRM155R71C104KA88D", lcsc="C71629", stock=2751535
            )
        },
    )
    pkg = bom.build_assembly(
        board / "board-bom.csv", board / "positions.csv", tmp_path, house="jlcpcb", lcsc=True
    )
    rows = _rows(tmp_path / "jlcpcb" / "bom.csv")
    assert rows[0]["LCSC Part #"] == "C71629"
    # The ESP32 and the MPN-less connector both resolved to nothing, and both
    # are lines JLCPCB cannot build.
    assert any("cannot be assembled" in w for w in pkg.warnings)
    assert pkg.lcsc[0]["lcsc"] == "C71629"


@needs_translator
def test_zero_stock_is_called_out_separately(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        bom,
        "resolve_lcsc",
        lambda mpns, **_kw: {
            "GRM155R71C104KA88D": bom.LcscPart(
                mpn="GRM155R71C104KA88D", lcsc="C71629", stock=0
            )
        },
    )
    pkg = bom.build_assembly(
        board / "board-bom.csv", board / "positions.csv", tmp_path, house="jlcpcb", lcsc=True
    )
    assert any("zero LCSC stock" in w for w in pkg.warnings)


# ---------------------------------------------------------------------------
# Sourcing gaps
# ---------------------------------------------------------------------------


def test_sourcing_gaps_skips_without_a_schematic(tmp_path: Path) -> None:
    report = bom.sourcing_gaps(tmp_path / "absent.kicad_sch")
    assert report["skipped"] is True
    assert "stage6" in report["reason"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_assembly_needs_a_release_package(tmp_path: Path, capsys) -> None:
    from blpl.core import cli

    rc = cli.main(["bom-assembly", "--project-dir", str(tmp_path)])
    assert rc == 2
    assert "no release package" in capsys.readouterr().err


def test_cli_bom_check_needs_an_emitted_schematic(tmp_path: Path, capsys) -> None:
    from blpl.core import cli

    rc = cli.main(["bom-check", "--project-dir", str(tmp_path)])
    assert rc == 2
    assert "run stage6 first" in capsys.readouterr().err


@needs_translator
def test_cli_assembly_writes_both_houses(tmp_path: Path, capsys) -> None:
    from blpl.core import cli

    stamp = tmp_path / "release" / "2026-08-12_000000Z"
    (stamp / "bom").mkdir(parents=True)
    (stamp / "placement").mkdir(parents=True)
    (stamp / "bom" / "board-bom.csv").write_text(RELEASE_BOM, encoding="utf-8")
    (stamp / "placement" / "positions.csv").write_text(POSITIONS, encoding="utf-8")

    assert cli.main(["bom-assembly", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "jlcpcb:" in out and "pcbway:" in out
    assert (stamp / "assembly" / "jlcpcb" / "bom.csv").is_file()
    assert (stamp / "assembly" / "pcbway" / "bom.csv").is_file()


# ---------------------------------------------------------------------------
# The agent tools
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path):
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox

    return ToolContext(project_id="p", project_dir=tmp_path, sandbox=FilesystemSandbox(tmp_path))


def test_agent_tool_refuses_an_unknown_house(tmp_path: Path) -> None:
    import asyncio

    from app.agent.registry import ToolDenied, fab_tools

    spec = next(t for t in fab_tools() if t.name == "write_assembly_files")
    with pytest.raises(ToolDenied, match="house must be"):
        asyncio.run(spec.handler(_ctx(tmp_path), {"house": "oshpark"}))


def test_agent_tool_points_at_the_release_build(tmp_path: Path) -> None:
    import asyncio

    from app.agent.registry import fab_tools

    spec = next(t for t in fab_tools() if t.name == "write_assembly_files")
    with pytest.raises(FileNotFoundError, match="release"):
        asyncio.run(spec.handler(_ctx(tmp_path), {"house": "jlcpcb"}))


def test_agent_sourcing_tool_needs_an_emitted_board(tmp_path: Path) -> None:
    import asyncio

    from app.agent.registry import fab_tools

    spec = next(t for t in fab_tools() if t.name == "check_sourcing_gaps")
    with pytest.raises(FileNotFoundError, match="stage6"):
        asyncio.run(spec.handler(_ctx(tmp_path), {}))


def test_assembly_package_serialises(board: Path, tmp_path: Path) -> None:
    pkg = bom.build_assembly(board / "board-bom.csv", None, tmp_path, house="pcbway")
    json.loads(bom.dumps(pkg.to_dict()))


def test_stage1_marks_where_passive_attributes_came_from():
    """A number a model read out of the design document is worth more than one
    a regex guessed and less than one a distributor confirmed."""
    from blpl.core import stage1_resolve_bom as s1

    out = s1._post_process(
        {"rows": [
            {"local_id": "C1", "mpn": "X", "package": "0402", "value": "100nF",
             "voltage_v": 50, "dielectric": "X7R", "tolerance": 10, "safety_class": None},
            {"local_id": "U1", "mpn": "Y", "package": "QFN", "value": None},
        ]},
        "p",
    )
    assert out["rows"][0]["attribute_provenance"] == "design_document"
    # A part with no passive attributes makes no claim about them.
    assert "attribute_provenance" not in out["rows"][1]


def test_an_empty_safety_class_is_not_recorded_as_a_rating():
    """Blank means the document did not say, and must not survive as a string
    that looks like an answer."""
    from blpl.core import stage1_resolve_bom as s1

    out = s1._post_process(
        {"rows": [{"local_id": "C1", "mpn": "X", "package": "0402",
                   "value": "100nF", "safety_class": ""}]},
        "p",
    )
    assert "safety_class" not in out["rows"][0]
