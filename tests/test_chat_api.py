"""Design chat: the project tools, the proposal safety rules, and the turn API.

The LLM is replaced by a scripted adapter throughout — what is under test is
BLPL's half of the contract: which paths a tool will touch, what happens when a
proposal goes stale, and whether a turn's events reach a late subscriber.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import chat as chat_mod
from app.agent import ToolContext, ToolExecutor, default_tools
from app.chat import (
    ProposalStore,
    apply_proposal,
    history_to_messages,
    persist_messages,
)
from app.conversations import Conversation
from app.references import FilesystemSandbox, ReferenceManifest
from blpl.core.llm_chat import (
    Done,
    Msg,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
)
from conftest import sign_in


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "overview.md").write_text("# Board\n\nOne connector.\n", encoding="utf-8")
    (proj / ".pipeline" / "nets.json").write_text('{"nets": [{"name": "GND"}]}', encoding="utf-8")
    return proj


def _ctx(project: Path) -> ToolContext:
    return ToolContext(
        project_id="dev04",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=project)
        ),
    )


def _call(ctx: ToolContext, _tool: str, **args) -> ToolResultBlock:
    """Run one tool through the real executor — policy, sandbox and all.

    The tool name is positional-only so it cannot collide with an argument the
    tool itself takes (``name``, ``path``, …). Approval is auto-granted here;
    the approval path has its own tests.
    """
    async def approve(_request):
        return True

    executor = ToolExecutor(default_tools(), ctx, approve=approve)
    return asyncio.run(executor(ToolUseBlock(id="t1", name=_tool, input=args)))


# -- tools --------------------------------------------------------------------


def test_list_separates_design_documents_from_artifacts(project) -> None:
    out = json.loads(_call(_ctx(project), "list_project_files").content)
    assert out["design_documents"] == ["overview.md"]
    assert out["pipeline_artifacts"] == ["nets.json"]


def test_reading_a_design_document_records_its_hash_for_later(project) -> None:
    ctx = _ctx(project)
    res = _call(ctx, "read_project_file", path="overview.md")
    assert not res.is_error and "One connector." in res.content
    # The hash of what the model actually read is what a later proposal is
    # anchored to — not a fresh stat at propose time.
    assert "overview.md" in ctx.read_shas


def test_artifacts_are_readable_and_missing_ones_say_so(project) -> None:
    ctx = _ctx(project)
    assert '"GND"' in _call(ctx, "read_pipeline_artifact", name="nets.json").content
    missing = _call(ctx, "read_pipeline_artifact", name="bom.json")
    assert missing.is_error and "has that stage run" in missing.content


def test_a_huge_artifact_keeps_both_ends_and_says_what_it_dropped(project) -> None:
    """Head-only truncation was actively misleading: nets.json holds its
    findings at one end and ~100k of net list at the other, so a prefix read
    showed every net and none of the problems."""
    body = "HEAD-MARKER" + ("x" * 80_000) + "TAIL-MARKER"
    (project / ".pipeline" / "big.json").write_text(body, encoding="utf-8")
    res = _call(_ctx(project), "read_pipeline_artifact", name="big.json")
    assert len(res.content) < len(body)
    assert "HEAD-MARKER" in res.content and "TAIL-MARKER" in res.content
    assert "elided from the middle" in res.content


@pytest.mark.parametrize(
    "path", ["../secret.md", "sub/dir.md", ".hidden.md", "script.py", "/etc/passwd"]
)
def test_tools_refuse_paths_that_leave_the_project(project, path) -> None:
    res = _call(_ctx(project), "read_project_file", path=path)
    assert res.is_error


def test_tool_errors_come_back_as_results_so_the_model_can_recover(project) -> None:
    """A bad call must not end the turn — the model needs to see the mistake."""
    res = _call(_ctx(project), "read_project_file", path="nope.md")
    assert res.is_error and "no file" in res.content


def test_unknown_tool_names_are_reported(project) -> None:
    res = _call(_ctx(project), "delete_everything", path="x")
    assert res.is_error and "unknown tool" in res.content


def test_malformed_arguments_are_rejected_not_run_as_empty(project) -> None:
    """A turn cut off mid-JSON must not become a call with no arguments."""
    res = _call(_ctx(project), "read_project_file", __malformed_arguments__='{"pa')
    assert res.is_error and "not valid JSON" in res.content


# -- proposals ----------------------------------------------------------------


def test_propose_writes_nothing_until_accepted(project) -> None:
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    res = _call(
        ctx, "propose_file_edit", path="overview.md", new_content="# Board v2\n", rationale="retitle"
    )
    assert not res.is_error
    # The file on disk is untouched.
    assert (project / "overview.md").read_text() == "# Board\n\nOne connector.\n"
    pending = ProposalStore(project).pending()
    assert len(pending) == 1 and pending[0].path == "overview.md"


def test_accepting_writes_the_file(project) -> None:
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    _call(ctx, "propose_file_edit", path="overview.md", new_content="# Board v2\n", rationale="r")
    proposal = ProposalStore(project).pending()[0]

    ok, detail = apply_proposal(proposal, project, ctx.sandbox)
    assert ok, detail
    assert (project / "overview.md").read_text() == "# Board v2\n"


def test_a_proposal_goes_stale_when_the_user_edits_underneath_it(project) -> None:
    """The race the proposal model exists to prevent: the editor saves whole
    files and cannot learn that something changed, so a blind write would
    destroy work nobody agreed to lose."""
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    _call(ctx, "propose_file_edit", path="overview.md", new_content="# From chat\n", rationale="r")
    proposal = ProposalStore(project).pending()[0]

    (project / "overview.md").write_text("# Edited by hand\n", encoding="utf-8")

    ok, detail = apply_proposal(proposal, project, ctx.sandbox)
    assert not ok and "stale" in detail
    assert (project / "overview.md").read_text() == "# Edited by hand\n"


def test_a_proposal_can_create_a_new_file(project) -> None:
    ctx = _ctx(project)
    res = _call(ctx, "propose_file_edit", path="power.md", new_content="# Power\n", rationale="new")
    assert not res.is_error
    proposal = ProposalStore(project).pending()[0]
    assert proposal.base_sha is None

    ok, _ = apply_proposal(proposal, project, ctx.sandbox)
    assert ok and (project / "power.md").read_text() == "# Power\n"


def test_a_create_proposal_refuses_once_the_file_exists(project) -> None:
    ctx = _ctx(project)
    _call(ctx, "propose_file_edit", path="power.md", new_content="# A\n", rationale="r")
    proposal = ProposalStore(project).pending()[0]
    (project / "power.md").write_text("# Someone else got there first\n", encoding="utf-8")

    ok, detail = apply_proposal(proposal, project, ctx.sandbox)
    assert not ok and "created after" in detail


def test_a_no_op_edit_is_refused_rather_than_queued(project) -> None:
    ctx = _ctx(project)
    same = (project / "overview.md").read_text()
    res = _call(ctx, "propose_file_edit", path="overview.md", new_content=same, rationale="r")
    assert res.is_error and "nothing to propose" in res.content


def test_proposals_cannot_target_files_outside_the_project(project) -> None:
    for bad in ("../escape.md", "sub/x.md", "evil.py"):
        res = _call(_ctx(project), "propose_file_edit", path=bad, new_content="x", rationale="r")
        assert res.is_error


# -- history round-trip -------------------------------------------------------


def test_history_replays_tool_calls_not_just_prose(tmp_path) -> None:
    """Dropping tool blocks would leave assistant turns referencing calls the
    provider can no longer see — which providers reject."""
    conv = Conversation.create(tmp_path, title="t")
    conv.append("user", "what's in it?", {"blocks": [{"type": "text", "text": "what's in it?"}]})

    call = ToolUseBlock(id="t1", name="read_project_file", input={"path": "overview.md"})
    assistant = Msg(
        role="assistant",
        content=[TextBlock("Looking."), call],
        raw_provider="anthropic",
        raw_content=[{"type": "text", "text": "Looking."}],
    )
    tool_turn = Msg.tool_results([ToolResultBlock(tool_use_id="t1", content="# Board")])
    persist_messages(conv, [assistant, tool_turn], model="m", usage={"output_tokens": 5})

    messages = history_to_messages(conv.read_all())
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[1].tool_calls[0].name == "read_project_file"
    assert messages[1].raw_provider == "anthropic"      # provider bytes survive the round-trip
    assert messages[2].content[0].tool_use_id == "t1"


def test_error_records_are_kept_for_humans_but_not_replayed_as_context(tmp_path) -> None:
    conv = Conversation.create(tmp_path, title="t")
    conv.append("user", "hi", {"blocks": [{"type": "text", "text": "hi"}]})
    conv.append("error", "RuntimeError: provider exploded", {})
    assert [m.role for m in history_to_messages(conv.read_all())] == ["user"]


# -- the API ------------------------------------------------------------------


class _ScriptedAdapter:
    """Stands in for a provider. Replays one turn per call."""

    turns: list[list] = []

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self._n = 0

    async def stream_chat(self, messages, *, system="", tools=(), max_tokens=0):
        events = _ScriptedAdapter.turns[self._n]
        self._n += 1
        for event in events:
            yield event


def _script(monkeypatch, turns: list[list]) -> None:
    _ScriptedAdapter.turns = turns
    monkeypatch.setattr(chat_mod, "build_chat_adapter", _ScriptedAdapter)


@pytest.fixture
def chat_client(client):
    """A client holding one event loop open across requests.

    A chat turn is a background task; without a persistent portal the loop dies
    with the POST that started it and the turn is cancelled before it streams.
    """
    with client as c:
        yield c


def _ready(chat_client) -> None:
    sign_in(chat_client)
    chat_client.put("/api/settings/secrets/anthropic", json={"value": "sk-test"})
    chat_client.post("/api/projects/init", json={"name": "scratch"})
    chat_client.put("/api/projects/scratch/files/overview.md", json={"content": "# Board\n"})


def _new_conversation(chat_client) -> str:
    return chat_client.post("/api/projects/scratch/conversations", json={"title": "design"}).json()[
        "filename"
    ]


def _drain(chat_client, turn_id: str) -> list[dict]:
    with chat_client.stream("GET", f"/api/projects/scratch/chat/{turn_id}/events") as r:
        body = "".join(r.iter_text())
    return [json.loads(ln[5:]) for ln in body.splitlines() if ln.startswith("data:")]


def test_a_chat_turn_streams_and_persists_both_sides(chat_client, monkeypatch) -> None:
    _script(
        monkeypatch,
        [[TextDelta("Use a 10uF bulk cap."), Done("end_turn", Msg.assistant("Use a 10uF bulk cap."))]],
    )
    _ready(chat_client)
    filename = _new_conversation(chat_client)

    started = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "how much cap?"}
    )
    assert started.status_code == 200
    events = _drain(chat_client, started.json()["turn_id"])
    assert [e["type"] for e in events][:2] == ["start", "text_delta"]
    assert events[-1]["type"] == "done" and events[-1]["stop_reason"] == "end_turn"

    convo = chat_client.get(f"/api/projects/scratch/conversations/{filename}").json()
    roles = [e["role"] for e in convo["events"]]
    assert roles == ["user", "assistant"]
    assert convo["events"][1]["content"] == "Use a 10uF bulk cap."


def test_the_question_survives_a_failing_turn(chat_client, monkeypatch) -> None:
    """The user's message is persisted before the turn runs, so a provider
    blowing up costs the answer and never the question."""

    class _Exploding(_ScriptedAdapter):
        async def stream_chat(self, messages, *, system="", tools=(), max_tokens=0):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(chat_mod, "build_chat_adapter", _Exploding)
    _ready(chat_client)
    filename = _new_conversation(chat_client)

    started = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "hello?"}
    )
    events = _drain(chat_client, started.json()["turn_id"])
    assert events[-1]["type"] == "error" and "provider exploded" in events[-1]["detail"]

    convo = chat_client.get(f"/api/projects/scratch/conversations/{filename}").json()
    assert [e["role"] for e in convo["events"]] == ["user", "error"]
    assert convo["events"][0]["content"] == "hello?"


def test_a_proposal_surfaces_as_its_own_event_and_can_be_accepted(chat_client, monkeypatch) -> None:
    call = ToolUseBlock(
        id="t1",
        name="propose_file_edit",
        input={"path": "overview.md", "new_content": "# Board v2\n", "rationale": "retitle"},
    )
    _script(
        monkeypatch,
        [
            [
                ToolCall(id=call.id, name=call.name, input=call.input),
                Done("tool_use", Msg(role="assistant", content=[call])),
            ],
            [Done("end_turn", Msg.assistant("Proposed the retitle."))],
        ],
    )
    _ready(chat_client)
    filename = _new_conversation(chat_client)

    started = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "retitle it"}
    )
    events = _drain(chat_client, started.json()["turn_id"])
    proposals = [e for e in events if e["type"] == "proposal"]
    assert len(proposals) == 1
    pid = proposals[0]["proposal"]["id"]

    # Nothing written yet.
    assert chat_client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# Board\n"
    assert len(chat_client.get("/api/projects/scratch/proposals").json()) == 1

    decided = chat_client.post(f"/api/projects/scratch/proposals/{pid}", json={"action": "accept"})
    assert decided.status_code == 200 and decided.json()["status"] == "accepted"
    assert chat_client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# Board v2\n"
    assert chat_client.get("/api/projects/scratch/proposals").json() == []


def test_rejecting_leaves_the_file_alone(chat_client, monkeypatch) -> None:
    call = ToolUseBlock(
        id="t1",
        name="propose_file_edit",
        input={"path": "overview.md", "new_content": "# nope\n", "rationale": "r"},
    )
    _script(
        monkeypatch,
        [
            [ToolCall(id=call.id, name=call.name, input=call.input), Done("tool_use", Msg(role="assistant", content=[call]))],
            [Done("end_turn", Msg.assistant("ok"))],
        ],
    )
    _ready(chat_client)
    filename = _new_conversation(chat_client)
    started = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "go"}
    )
    events = _drain(chat_client, started.json()["turn_id"])
    pid = next(e for e in events if e["type"] == "proposal")["proposal"]["id"]

    assert chat_client.post(f"/api/projects/scratch/proposals/{pid}", json={"action": "reject"}).status_code == 200
    assert chat_client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# Board\n"
    # A decided proposal cannot be decided again.
    assert chat_client.post(f"/api/projects/scratch/proposals/{pid}", json={"action": "accept"}).status_code == 409


def test_accepting_a_stale_proposal_is_a_409_not_a_clobber(chat_client, monkeypatch) -> None:
    call = ToolUseBlock(
        id="t1",
        name="propose_file_edit",
        input={"path": "overview.md", "new_content": "# from chat\n", "rationale": "r"},
    )
    _script(
        monkeypatch,
        [
            [ToolCall(id=call.id, name=call.name, input=call.input), Done("tool_use", Msg(role="assistant", content=[call]))],
            [Done("end_turn", Msg.assistant("ok"))],
        ],
    )
    _ready(chat_client)
    filename = _new_conversation(chat_client)
    started = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "go"}
    )
    events = _drain(chat_client, started.json()["turn_id"])
    pid = next(e for e in events if e["type"] == "proposal")["proposal"]["id"]

    chat_client.put("/api/projects/scratch/files/overview.md", json={"content": "# hand edit\n"})

    r = chat_client.post(f"/api/projects/scratch/proposals/{pid}", json={"action": "accept"})
    assert r.status_code == 409 and "stale" in r.json()["detail"]
    assert chat_client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# hand edit\n"


def test_chat_needs_a_provider_key_and_a_real_conversation(chat_client, monkeypatch) -> None:
    sign_in(chat_client)
    chat_client.post("/api/projects/init", json={"name": "scratch"})
    filename = chat_client.post(
        "/api/projects/scratch/conversations", json={"title": "d"}
    ).json()["filename"]

    # No key stored: refuse up front with the resolver's message.
    r = chat_client.post(
        f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "hi"}
    )
    assert r.status_code == 400 and "key" in r.json()["detail"].lower()

    chat_client.put("/api/settings/secrets/anthropic", json={"value": "sk-test"})
    assert (
        chat_client.post(
            "/api/projects/scratch/conversations/does-not-exist.jsonl/chat", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert (
        chat_client.post(
            f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "   "}
        ).status_code
        == 400
    )


def test_chat_endpoints_respect_the_session_gate(client) -> None:
    assert client.get("/api/projects/scratch/proposals").status_code == 401
    assert client.post("/api/projects/scratch/proposals/prop_x", json={"action": "accept"}).status_code == 401
