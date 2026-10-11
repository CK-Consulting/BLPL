"""The agent-notes write boundary, enforced on a real tool call.

blpl.core.agent_notes, the scope, the mailbox and the gate, is tested in the
pipeline's own suite (test_agent_notes.py). This is the other half: the scope
has to bite inside the app's tool executor, where writes actually happen. Split
out when the pipeline and the app became separate trees.
"""

from __future__ import annotations

A = "a1b2c3d4"


def test_the_boundary_is_enforced_on_a_real_tool_call(tmp_path):
    """The scope has to bite where writes actually happen, not only in its own
    unit test. propose_file_edit is the one writer agents have."""
    import asyncio

    from blpl.core.agent_notes import WriteScope
    from app.agent import default_tools
    from app.agent.executor import ToolExecutor
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox, ReferenceManifest
    from blpl.core.llm_chat import ToolUseBlock

    project = tmp_path / "proj"
    base, sensor = project / "base", project / "sensor"
    sensor.mkdir(parents=True)
    base.mkdir(parents=True)
    (base / "power.md").write_text("# power\n")
    (sensor / "sensor.md").write_text("# sensor\n")

    ctx = ToolContext(
        project_id="proj",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest(project_id="proj", workspace_root=project)
        ),
        conversation="c.jsonl",
        write_scope=WriteScope(
            project_dir=project, board_dir=base, identifier=A, other_boards=(sensor,)
        ),
    )
    ex = ToolExecutor(default_tools(), ctx)

    def call(path: str):
        block = ToolUseBlock(
            id="t1",
            name="propose_file_edit",
            input={"path": path, "new_content": "x", "rationale": "y"},
        )
        return asyncio.run(ex(block))

    # Its own board: allowed through the boundary (may still fail later for
    # unrelated reasons — what matters is that it was not refused as trespass).
    own = call("base/power.md")
    assert "another board's file" not in own.content

    # Another board's design file: refused, and told what to do instead.
    other = call("sensor/sensor.md")
    assert other.is_error
    assert "another board's file" in other.content
    assert "Append a note" in other.content
