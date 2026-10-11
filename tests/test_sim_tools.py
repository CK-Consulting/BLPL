"""The simulate_subcircuits agent tool.

blpl.agent.tools.spice is tested in the pipeline's own suite (test_spice.py);
this is the app registry's wrapper around it. Split out when the pipeline and
the app became separate trees.
"""

from __future__ import annotations

import pytest


def test_agent_tool_points_at_stage8_when_there_is_nothing_to_simulate(tmp_path) -> None:
    import asyncio

    from app.agent.registry import sim_tools
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox

    spec = next(t for t in sim_tools() if t.name == "simulate_subcircuits")
    ctx = ToolContext(
        project_id="p",
        project_dir=tmp_path,
        sandbox=FilesystemSandbox(tmp_path),
    )
    with pytest.raises(FileNotFoundError, match="stage8"):
        asyncio.run(spec.handler(ctx, {}))
