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
    # A board with a net table refers to nets by number only (Codex, PR #50).
    assert sexpr.find(via, "net") == ["net", "1"]
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
    assert {"make_edit_copies", "propose_kicad_edit", "run_checks_on_edit", "read_run_log"} <= names


def test_a_check_that_writes_no_report_is_a_failure_not_a_clean_result(project, monkeypatch):
    from blpl.core import stage7_validate

    ctx = _ctx(project)
    run(edit_copies._make_edit_copies(ctx, {}))
    monkeypatch.setattr(stage7_validate, "_run_drc", lambda pcb, out: {
        "ok": False, "skipped": False, "exit_code": 3, "stderr_tail": "Failed to load board"})
    out = run(edit_copies._run_checks_on_edit(ctx, {"path": "kicad/b_edit.kicad_pcb"}))
    assert "FAILED" in out and "exited 3" in out and "Failed to load board" in out
    assert "0 violation" not in out


def test_a_run_log_is_readable_for_this_project_only(client, tmp_path, monkeypatch):
    from conftest import init_body, sign_in

    from app.db import SessionFactory
    from app.models import Project, Run

    sign_in(client)
    client.post("/api/projects/init", json=init_body("mine"))
    monkeypatch.setenv("BLPL_DATA_ROOT", str(tmp_path))
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "run_abc.log").write_text("==> stage7\nstage7: FAIL drc\n")
    with SessionFactory() as s:
        from sqlalchemy import select

        pid = s.scalar(select(Project.id).where(Project.name == "mine"))
        s.add(Run(id="run_abc", project_id=pid, kind="pipeline", cmd=[], status="done", exit_code=1))
        s.commit()
    mine = ToolContext(project_id="mine", project_dir=tmp_path,
                       sandbox=FilesystemSandbox(manifest=ReferenceManifest(project_id="mine", workspace_root=tmp_path)),
                       conversation="c.jsonl")
    out = run(edit_copies._read_run_log(mine, {"run_id": "run_abc"}))
    assert "exit 1" in out and "stage7: FAIL drc" in out
    other = ToolContext(project_id="theirs", project_dir=tmp_path,
                        sandbox=FilesystemSandbox(manifest=ReferenceManifest(project_id="theirs", workspace_root=tmp_path)),
                        conversation="c.jsonl")
    with pytest.raises(ToolDenied, match="no run"):
        run(edit_copies._read_run_log(other, {"run_id": "run_abc"}))


def test_a_symlinked_emitted_file_is_never_copied(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.kicad_pcb"
    outside.write_text("(kicad_pcb (secret))")
    (project / "kicad/leak.kicad_pcb").symlink_to(outside)
    with pytest.raises(ToolDenied, match="symbolic link"):
        run(edit_copies._make_edit_copies(_ctx(project), {}))
    assert not (project / "kicad/leak_edit.kicad_pcb").exists()


def test_schematic_parity_findings_are_reported(project, monkeypatch):
    import json as _json

    from blpl.core import stage7_validate

    ctx = _ctx(project)
    run(edit_copies._make_edit_copies(ctx, {}))

    def fake_drc(pcb, out):
        out.write_text(_json.dumps({"violations": [], "unconnected_items": [], "schematic_parity": [
            {"type": "missing_footprint", "description": "Missing footprint R5", "items": []}]}))
        return {"ok": False, "skipped": False, "exit_code": 5}

    monkeypatch.setattr(stage7_validate, "_run_drc", fake_drc)
    out = run(edit_copies._run_checks_on_edit(ctx, {"path": "kicad/b_edit.kicad_pcb"}))
    assert "1 schematic parity finding(s)" in out and "Missing footprint R5" in out


def test_kicad_edit_proposals_are_shown_live():
    from app import chat

    assert "propose_kicad_edit" in chat._PROPOSING_TOOLS


def test_a_multi_board_project_copies_the_named_boards_published_files(tmp_path):
    for b in ("core", "sb-ant"):
        (tmp_path / b / "kicad").mkdir(parents=True)
        (tmp_path / b / "kicad" / f"{b}.kicad_pcb").write_text(BOARD)
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolDenied, match="core, sb-ant"):
        run(edit_copies._make_edit_copies(ctx, {}))
    run(edit_copies._make_edit_copies(ctx, {"board": "sb-ant"}))
    assert (tmp_path / "sb-ant/kicad/sb-ant_edit.kicad_pcb").exists()
    assert not (tmp_path / "core/kicad/core_edit.kicad_pcb").exists()
    out = run(edit_copies._propose_kicad_edit(ctx, {
        "path": "sb-ant/kicad/sb-ant_edit.kicad_pcb", "operations": [{"op": "delete", "uuid": "seg-1"}]}))
    assert "Proposed an edit to sb-ant/kicad/sb-ant_edit.kicad_pcb" in out


def test_an_edit_copy_that_is_a_link_is_never_written_through(project):
    (project / "board.md").write_text("design")
    (project / "kicad/b_edit.kicad_pcb").symlink_to(project / "board.md")
    with pytest.raises(ToolDenied, match="not written through"):
        run(edit_copies._make_edit_copies(_ctx(project), {"overwrite": True}))
    assert (project / "board.md").read_text() == "design"
