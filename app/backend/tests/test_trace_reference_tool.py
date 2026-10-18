"""The agent's reference-circuit tool."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.agent import ToolContext
from app.agent.registry import _trace_reference_circuit, default_tools
from app.agent.toolspec import ToolDenied
from app.references import FilesystemSandbox, ReferenceManifest


def _ctx(project: Path) -> ToolContext:
    return ToolContext(project_id="p", project_dir=project,
                       sandbox=FilesystemSandbox(manifest=ReferenceManifest(project_id="p", workspace_root=project)),
                       conversation="c.jsonl")


def test_the_tool_is_registered():
    assert "trace_reference_circuit" in {t.name for t in default_tools()}


def test_only_schematics_are_traced(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    with pytest.raises(ToolDenied, match="not a KiCad schematic"):
        asyncio.run(_trace_reference_circuit(_ctx(tmp_path), {"path": "notes.md", "part": "U1"}))


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_a_schematic_with_no_such_part_is_refused_with_the_reason(tmp_path):
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "r.kicad_sch").write_text(
        '(kicad_sch (version 20250114) (generator "eeschema") (uuid "a") (paper "A4") (lib_symbols))')
    with pytest.raises(ToolDenied):
        asyncio.run(_trace_reference_circuit(_ctx(tmp_path), {"path": "references/r.kicad_sch", "part": "U1"}))


def _root_naming(tmp_path: Path, child_dir: str) -> None:
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "root.kicad_sch").write_text(
        '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))')
    (tmp_path / "design" / "child.kicad_sch").write_text("(kicad_sch child)")


def test_an_ignored_sheet_named_by_a_trusted_root_is_refused(tmp_path):
    from app import llm_ignore

    _root_naming(tmp_path, "design")
    llm_ignore.set_ignored(tmp_path, "design/child.kicad_sch", True)
    with pytest.raises(ToolDenied, match="design/child.kicad_sch is on the user's LLM-ignore list"):
        asyncio.run(_trace_reference_circuit(_ctx(tmp_path), {"path": "design/root.kicad_sch", "part": "U1"}))


def test_a_linked_sheet_is_never_read(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("victim") / "secret.kicad_sch"
    outside.write_text("(kicad_sch secret)")
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "root.kicad_sch").write_text(
        '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))')
    (tmp_path / "design" / "child.kicad_sch").symlink_to(outside)
    with pytest.raises(ToolDenied, match="not beside it"):
        asyncio.run(_trace_reference_circuit(_ctx(tmp_path), {"path": "design/root.kicad_sch", "part": "U1"}))
