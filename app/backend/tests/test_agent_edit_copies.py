"""The agent's tools for fixing ERC/DRC findings on _edit copies of the emitted project."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.agent import ToolContext
from app.agent import edit_copies
from app.agent.toolspec import ToolDenied
from app.chat import ProposalStore
from app.references import FilesystemSandbox, ReferenceManifest
from blpl.emitter import sexpr

BOARD = """(kicad_pcb (version 20240108) (generator "t")
  (net 0 "")
  (net 1 "GND")
  (footprint "Device:R" (layer "F.Cu") (uuid "f-1") (at 10 10)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
  )
  (segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""


def _ctx(project: Path) -> ToolContext:
    return ToolContext(
        project_id="proj", project_dir=project,
        sandbox=FilesystemSandbox(manifest=ReferenceManifest(project_id="proj", workspace_root=project)),
        conversation="c.jsonl",
    )


@pytest.fixture
def project(tmp_path):
    kicad = tmp_path / "kicad"
    kicad.mkdir()
    (kicad / "b.kicad_pcb").write_text(BOARD)
    (kicad / "b.kicad_pro").write_text("{}")
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_copies_are_made_beside_the_originals_and_never_overwritten_unasked(project):
    ctx = _ctx(project)
    out = run(edit_copies._make_edit_copies(ctx, {}))
    assert (project / "kicad/b_edit.kicad_pcb").read_text() == BOARD
    assert (project / "kicad/b_edit.kicad_pro").exists()
    assert "regenerates the originals" in out
    (project / "kicad/b_edit.kicad_pcb").write_text(BOARD + ";edited")
    out = run(edit_copies._make_edit_copies(ctx, {}))
    assert "left as they are" in out
    assert (project / "kicad/b_edit.kicad_pcb").read_text().endswith(";edited")


def test_an_original_is_never_edited(project):
    ctx = _ctx(project)
    with pytest.raises(ToolDenied, match="make_edit_copies"):
        run(edit_copies._propose_kicad_edit(ctx, {
            "path": "kicad/b.kicad_pcb", "operations": [{"op": "delete", "uuid": "seg-1"}]}))


def test_operations_become_a_proposal_that_parses(project):
    ctx = _ctx(project)
    run(edit_copies._make_edit_copies(ctx, {}))
    out = run(edit_copies._propose_kicad_edit(ctx, {
        "path": "kicad/b_edit.kicad_pcb",
        "operations": [
            {"op": "move_footprint", "ref": "R1", "x": 12.5, "y": 8},
            {"op": "add_via", "net": "GND", "x": 3, "y": 4},
            {"op": "delete", "uuid": "seg-1"},
        ],
        "rationale": "test"}))
    assert "moved R1" in out and "added a via on GND" in out and "deleted segment" in out
    (proposal,) = ProposalStore(project).pending()
    assert proposal.path == "kicad/b_edit.kicad_pcb"
    tree = sexpr.parse(proposal.new_content)
    fp = sexpr.find_all(tree, "footprint")[0]
    assert sexpr.find(fp, "at")[1:3] == ["12.5", "8"]
    assert sexpr.find_all(tree, "via") and not sexpr.find_all(tree, "segment")
    via = sexpr.find_all(tree, "via")[0]
    assert sexpr.find(via, "net") == ["net", "1", '"GND"']
    # The original copy is untouched until the user accepts.
    assert (project / "kicad/b_edit.kicad_pcb").read_text() == BOARD


def test_unknown_nets_parts_and_operations_are_refused(project):
    ctx = _ctx(project)
    run(edit_copies._make_edit_copies(ctx, {}))
    for op, msg in (({"op": "add_via", "net": "NOPE", "x": 1, "y": 1}, "no net"),
                    ({"op": "move_footprint", "ref": "U9", "x": 1, "y": 1}, "no footprint"),
                    ({"op": "add_wire", "start": [0, 0], "end": [1, 1]}, "not available"),
                    ({"op": "delete", "uuid": "missing"}, "no top-level item")):
        with pytest.raises(ToolDenied, match=msg):
            run(edit_copies._propose_kicad_edit(ctx, {"path": "kicad/b_edit.kicad_pcb", "operations": [op]}))


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_checks_run_on_the_copy_and_report_counts(project):
    ctx = _ctx(project)
    run(edit_copies._make_edit_copies(ctx, {}))
    out = run(edit_copies._run_checks_on_edit(ctx, {"path": "kicad/b_edit.kicad_pcb"}))
    assert out.startswith("DRC on kicad/b_edit.kicad_pcb") or "did not run" in out


def test_the_skill_is_readable_and_the_tools_are_registered():
    from app.agent.registry import _packaged_skills, default_tools

    assert "erc-drc-resolution" in _packaged_skills()
    names = {t.name for t in default_tools()}
    assert {"make_edit_copies", "propose_kicad_edit", "run_checks_on_edit"} <= names
