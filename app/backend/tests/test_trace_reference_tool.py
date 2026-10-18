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
