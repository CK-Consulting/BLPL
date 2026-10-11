"""The KiCad bridge: the allowlist, the contract pin, and the undo anchor.

No live MCP server here — a fake one exercises the parts BLPL owns. What matters
is that the bridge decides its own policy rather than inheriting the server's,
refuses a half-set instead of exposing one, and never lets a mutation happen
without an anchor to revert to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.agent import kicad_bridge as kb
from app.agent.mcp_client import MCPTool


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    proj = tmp_path / "board"
    proj.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=proj, check=True, capture_output=True)
    (proj / "board.kicad_pcb").write_text("(kicad_pcb original)\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=proj, check=True, capture_output=True)
    return proj


def _fake_client(tool_names: list[str], monkeypatch, calls: list | None = None):
    class FakeClient:
        def __init__(self, url, **kw):
            self.url = url

        def initialize(self):
            return {}

        def list_tools(self):
            return [
                MCPTool(name=n, description=f"{n} does a thing", input_schema={"type": "object"})
                for n in tool_names
            ]

        def call_tool(self, name, arguments):
            if calls is not None:
                calls.append((name, arguments))
            return f"{name} ok"

    monkeypatch.setattr(kb, "MCPClient", FakeClient)
    return FakeClient


# -- the contract pin ---------------------------------------------------------


def test_a_complete_server_enables_the_bridge(monkeypatch) -> None:
    _fake_client(sorted(kb.REQUIRED), monkeypatch)
    status, usable = kb.probe("http://kcaa:9000/mcp")
    assert status.available and status.tools == len(kb.REQUIRED)
    assert set(usable) == kb.REQUIRED


def test_a_server_missing_tools_disables_the_bridge_and_names_them(monkeypatch) -> None:
    """Discovering there are no vias three-quarters through routing a board is
    worse than not starting, so a partial set is refused with the list."""
    partial = sorted(kb.REQUIRED - {"pcb_add_vias", "add_zone"})
    _fake_client(partial, monkeypatch)
    status, _ = kb.probe("http://kcaa:9000/mcp")
    assert not status.available
    assert status.missing == ["add_zone", "pcb_add_vias"]
    assert "older version" in status.error


def test_an_unreachable_server_reports_why(monkeypatch) -> None:
    class Dead:
        def __init__(self, url, **kw):
            pass

        def initialize(self):
            raise kb.MCPError("connection refused")

    monkeypatch.setattr(kb, "MCPClient", Dead)
    status, usable = kb.probe("http://nope:9000/mcp")
    assert not status.available and "connection refused" in status.error and usable == []


def test_the_policy_table_matches_the_contract_test() -> None:
    """tests/kcaa_contract_check.py pins the same names against a real kcaa
    checkout, so an upstream rename fails in CI rather than when someone asks
    the agent to route a board."""
    source = (Path(__file__).parent / "kcaa_contract_check.py").read_text(encoding="utf-8")
    pinned = {
        line.strip().strip('",')
        for line in source.split("REQUIRED_TOOLS = {", 1)[1].split("}", 1)[0].splitlines()
        if line.strip().startswith('"')
    }
    assert pinned == set(kb.POLICY), "the bridge allowlist and the contract test have drifted apart"


# -- policy is ours, not the server's -----------------------------------------


def test_only_allowlisted_tools_are_exposed(monkeypatch) -> None:
    """A tool server is software on the other side of a socket; a new upstream
    tool is a decision to make, not a capability to inherit."""
    _fake_client(sorted(kb.REQUIRED) + ["delete_everything", "upload_to_pastebin"], monkeypatch)
    _status, usable = kb.probe("http://kcaa:9000/mcp")
    names = {s.name for s in kb.build_tools("http://kcaa:9000/mcp", usable)}
    assert "delete_everything" not in names and "upload_to_pastebin" not in names


def test_reads_are_free_and_writes_are_not(monkeypatch) -> None:
    _fake_client(sorted(kb.REQUIRED), monkeypatch)
    _status, usable = kb.probe("http://kcaa:9000/mcp")
    specs = {s.name: s for s in kb.build_tools("http://kcaa:9000/mcp", usable)}
    assert specs["get_ratsnest"].approval == "auto"
    # Routing is hundreds of calls; a confirmation per track would be unusable,
    # which is its own failure — a dialog nobody can answer gets answered blindly.
    assert specs["pcb_route_pad_to_pad"].approval == "ask"
    assert specs["pcb_route_pad_to_pad"].kind == "kicad_mutation"
    # Structural changes are rare and sweeping, so they ask every time.
    assert specs["add_zone"].approval == "ask_always"
    assert specs["revert_kicad_edits"].approval == "ask_always"


# -- the undo anchor ----------------------------------------------------------


def test_a_snapshot_survives_edits_and_restores_them(git_project) -> None:
    commit = kb.snapshot(git_project)
    assert commit

    (git_project / "board.kicad_pcb").write_text("(kicad_pcb ruined by routing)\n", encoding="utf-8")
    ok, detail = kb.restore(git_project)
    assert ok, detail
    assert (git_project / "board.kicad_pcb").read_text() == "(kicad_pcb original)\n"


def test_the_snapshot_leaves_no_trace_in_history(git_project) -> None:
    """It is an undo anchor, not a commit — it must not appear in the project's
    history or move HEAD, or every routing attempt would litter the log."""
    before = subprocess.run(
        ["git", "log", "--oneline"], cwd=git_project, capture_output=True, text=True
    ).stdout
    kb.snapshot(git_project)
    after = subprocess.run(
        ["git", "log", "--oneline"], cwd=git_project, capture_output=True, text=True
    ).stdout
    assert before == after


def test_a_project_without_git_still_works_it_just_has_no_undo(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert kb.snapshot(plain) is None
    ok, _ = kb.restore(plain)
    assert not ok


def test_highlight_needs_something_to_point_at(tmp_path) -> None:
    import asyncio

    from app.agent import ToolContext
    from app.agent.toolspec import ToolDenied
    from app.references import FilesystemSandbox, ReferenceManifest

    events: list[dict] = []
    ctx = ToolContext(
        project_id="p",
        project_dir=tmp_path,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="p", workspace_root=tmp_path)
        ),
        on_ui=events.append,
    )
    spec = kb.highlight_tool()
    asyncio.run(spec.handler(ctx, {"designators": ["U3"], "nets": ["VBUS"]}))
    assert events == [{"type": "highlight", "designators": ["U3"], "nets": ["VBUS"]}]

    with pytest.raises(ToolDenied):
        asyncio.run(spec.handler(ctx, {}))
