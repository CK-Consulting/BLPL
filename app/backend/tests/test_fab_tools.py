"""The fabrication agent tools: write_assembly_files and check_sourcing_gaps.

What they wrap, blpl.agent.tools.bom, is tested in the pipeline's own suite
(test_bom.py). These are the app's side of it: that the registry exposes the
tools, refuses what it should, and points the model at the stage that would fix
a miss. Split out when the pipeline and the app became separate trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest


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
