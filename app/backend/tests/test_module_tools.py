"""The module-extraction agent tools a conversation actually calls.

The importer and the extraction itself are tested in the pipeline's own suite
(test_module_extraction.py); these are the registry's wrappers around them, and
the sandbox they run inside. Split out when the pipeline and the app became
separate trees.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# The same tiny two-block board the pipeline suite reads: a charger (U1 + C1)
# feeding an MCU (U2). VBAT and I2C cross between them; CHG_INT is internal to
# the charger.
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


def test_extraction_copies_a_footprint_from_the_projects_own_libraries(board_dir, tmp_path) -> None:
    """Stock was the only footprint root the tool passed, so every project
    footprint a module used was left behind with a 'not found' note."""
    import asyncio

    project = tmp_path / "carrier"
    project.mkdir()
    shutil.copytree(board_dir, project / "src")
    fp = project / "libraries" / "footprints" / "Package_QFN.pretty"
    fp.mkdir(parents=True)
    (fp / "QFN-28.kicad_mod").write_text('(footprint "QFN-28")', encoding="utf-8")
    ctx = _ctx(project)
    asyncio.run(_tool("read_kicad_design").handler(ctx, {"directory": "src"}))

    out = json.loads(asyncio.run(_tool("extract_module").handler(ctx, {"refdes": ["U1"], "name": "chg"})))
    assert (project / "modules" / "chg" / "Package_QFN.pretty" / "QFN-28.kicad_mod").is_file()
    assert not any("QFN-28" in n for n in out["notes"])
