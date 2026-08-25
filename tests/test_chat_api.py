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
from conftest import give_endpoint, sign_in


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


@pytest.mark.parametrize("path", ["../secret.md", "../../etc/passwd", "/etc/passwd"])
def test_tools_refuse_paths_that_leave_the_project(project, path) -> None:
    """The project directory is the boundary, and it is the only one.

    These must fail because they leave it — not because the file is missing.
    Asserting the reason is the point: a check that only ever fires on
    non-existent paths would pass just as happily with no check at all.
    """
    res = _call(_ctx(project), "read_project_file", path=path)
    assert res.is_error
    # Refused for leaving the boundary — the sandbox gets there first, before
    # the handler is ever entered — and specifically not "no such file", which
    # is what a check that had quietly stopped working would say.
    assert "refused" in res.content and "no file" not in res.content


def test_a_symlink_out_of_the_project_is_refused(project, tmp_path) -> None:
    """The string check cannot see this one; the resolved path can."""
    (tmp_path / "secret.md").write_text("elsewhere", encoding="utf-8")
    (project / "escape.md").symlink_to(tmp_path / "secret.md")
    res = _call(_ctx(project), "read_project_file", path="escape.md")
    assert res.is_error and "elsewhere" not in res.content


def test_files_in_subdirectories_are_readable(project) -> None:
    """A multi-board project keeps each board in its own directory, so a rule of
    'bare filenames in the project root' made the boards unreadable to the
    assistant working on them."""
    (project / "sensor").mkdir()
    (project / "sensor" / "board.md").write_text("# Sensor\n", encoding="utf-8")
    res = _call(_ctx(project), "read_project_file", path="sensor/board.md")
    assert not res.is_error and "# Sensor" in res.content


def test_non_markdown_text_in_the_project_is_readable(project) -> None:
    (project / "power-budget.csv").write_text("rail,mA\n3V3,420\n", encoding="utf-8")
    res = _call(_ctx(project), "read_project_file", path="power-budget.csv")
    assert not res.is_error and "3V3,420" in res.content


def test_binary_files_say_what_to_do_instead_of_returning_mojibake(project) -> None:
    (project / "datasheets").mkdir()
    (project / "datasheets" / "LM317.pdf").write_bytes(b"%PDF-1.4 \x00\x01binary")
    res = _call(_ctx(project), "read_project_file", path="datasheets/LM317.pdf")
    assert res.is_error and "extract_datasheet_specs" in res.content


def test_an_oversized_file_is_refused_with_its_size(project) -> None:
    (project / "huge.md").write_text("x" * (300 * 1024), encoding="utf-8")
    res = _call(_ctx(project), "read_project_file", path="huge.md")
    assert res.is_error and "300 KB" in res.content


def test_context_ignore_is_listed_by_name_but_kept_out_of_the_project_files(project) -> None:
    """The folder exists so files can stay in the project without joining the
    design conversation. Names are listed — without them 'look at the enclosure
    drawing' has nothing to resolve against — but they are separated, and the
    note carries the instruction."""
    (project / "context-ignore").mkdir()
    (project / "context-ignore" / "enclosure.md").write_text("# Case\n", encoding="utf-8")
    out = json.loads(_call(_ctx(project), "list_project_files").content)
    assert out["context_ignore"]["files"] == ["context-ignore/enclosure.md"]
    assert "unless the user asks" in out["context_ignore"]["note"]
    assert not any("context-ignore" in p for p in out["other_project_files"])
    assert out["design_documents"] == ["overview.md"]


def test_context_ignore_files_are_still_readable_when_asked_for(project) -> None:
    """Not a permission boundary — an attention boundary. Making it unreadable
    would mean the one time the user does want it looked at, they cannot ask."""
    (project / "context-ignore").mkdir()
    (project / "context-ignore" / "enclosure.md").write_text("# Case\n", encoding="utf-8")
    res = _call(_ctx(project), "read_project_file", path="context-ignore/enclosure.md")
    assert not res.is_error and "# Case" in res.content


def test_listing_reaches_into_sub_board_directories(project) -> None:
    (project / "sensor").mkdir()
    (project / "sensor" / "board.md").write_text("# Sensor\n", encoding="utf-8")
    out = json.loads(_call(_ctx(project), "list_project_files").content)
    assert "sensor/board.md" in out["other_project_files"]


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


def test_a_question_nothing_answered_is_kept_for_humans_and_not_replayed(tmp_path) -> None:
    """Both halves go: the error marker, and the question it was the only reply
    to. This used to keep the question, which is benign until the failure is
    about request size — then every retry adds another copy of the message that
    was already too big, and the request grows with each attempt to escape it.

    On disk and on screen it stays. The transcript is the record of what
    happened, and it is where the four identical copies explain themselves."""
    conv = Conversation.create(tmp_path, title="t")
    conv.append("user", "hi", {"blocks": [{"type": "text", "text": "hi"}]})
    conv.append("error", "RuntimeError: provider exploded", {})
    assert history_to_messages(conv.read_all()) == []
    assert len(conv.read_all()) == 2


def test_a_question_that_was_answered_before_the_failure_is_replayed(tmp_path) -> None:
    """A provider dying mid-answer still read the question, and the partial
    reply on screen refers to it."""
    conv = Conversation.create(tmp_path, title="t")
    conv.append("user", "hi", {"blocks": [{"type": "text", "text": "hi"}]})
    conv.append("assistant", "partway through", {"blocks": [{"type": "text", "text": "partway"}]})
    conv.append("error", "RuntimeError: provider exploded", {})
    assert [m.role for m in history_to_messages(conv.read_all())] == ["user", "assistant"]


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
    give_endpoint()
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



def _read_then(call: ToolUseBlock) -> list:
    """A scripted turn that reads the file before proposing an edit to it.

    propose_file_edit refuses an edit with no read behind it in the same turn,
    which is the rule that stops a model rewriting a file from a stale copy in
    its context. These turns have to satisfy it the way a real one does.
    """
    read = ToolUseBlock(
        id="t0", name="read_project_file", input={"path": call.input["path"]}
    )
    return [
        ToolCall(id=read.id, name=read.name, input=read.input),
        Done("tool_use", Msg(role="assistant", content=[read])),
    ]

def test_a_proposal_surfaces_as_its_own_event_and_can_be_accepted(chat_client, monkeypatch) -> None:
    call = ToolUseBlock(
        id="t1",
        name="propose_file_edit",
        input={"path": "overview.md", "new_content": "# Board v2\n", "rationale": "retitle"},
    )
    _script(
        monkeypatch,
        [
            _read_then(call),
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
            _read_then(call),
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
            _read_then(call),
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


def test_the_most_recently_active_conversation_comes_first(tmp_path) -> None:
    """Reopening the workbench should land in the thread you were last working
    in. The list was ordered newest-first by filename while the client took the
    *tail*, so every reload dropped you into the first conversation the project
    ever had."""
    from app.conversations import Conversation, list_conversations

    old = Conversation.create(tmp_path, title="old")
    new = Conversation.create(tmp_path, title="new")
    old.append("user", "still working here")   # older file, newer activity

    order = [m.filename for m in list_conversations(tmp_path)]
    assert order[0] == old.path.name, order
    assert new.path.name in order


def test_archiving_takes_a_conversation_off_the_list_without_destroying_it(tmp_path) -> None:
    """A transcript records what was proposed and why a part was chosen, which
    outlives its usefulness in a dropdown. Archiving hides it; nothing deletes
    it, and the file stays readable straight off disk."""
    from app.conversations import Conversation, list_conversations, set_archived

    keep = Conversation.create(tmp_path, title="keep")
    done = Conversation.create(tmp_path, title="done")
    done.append("user", "settled")

    moved = set_archived(tmp_path, done.path.name, True)
    assert moved.is_file() and moved.parent.name == "archived"
    assert not (tmp_path / done.path.name).exists()

    visible = [m.filename for m in list_conversations(tmp_path)]
    assert visible == [keep.path.name]

    everything = {m.filename: m.archived for m in list_conversations(tmp_path, include_archived=True)}
    assert everything[done.path.name] is True
    assert everything[keep.path.name] is False

    # …and it comes back intact, messages included.
    set_archived(tmp_path, done.path.name, False)
    back = Conversation.open_existing(tmp_path, done.path.name)
    assert [e["content"] for e in back.read_all()] == ["settled"]


def test_archiving_something_twice_is_not_an_error(tmp_path) -> None:
    """Two tabs, two clicks. The second should agree rather than 404."""
    from app.conversations import Conversation, set_archived

    conv = Conversation.create(tmp_path, title="x")
    set_archived(tmp_path, conv.path.name, True)
    assert set_archived(tmp_path, conv.path.name, True).parent.name == "archived"


def test_a_crafted_filename_cannot_move_files_around(tmp_path) -> None:
    import pytest

    from app.conversations import set_archived

    for bad in ("../secret.jsonl", "nope.txt", "/etc/passwd"):
        with pytest.raises((ValueError, FileNotFoundError)):
            set_archived(tmp_path, bad, True)


# -- per-part datasheet folders -----------------------------------------------


def test_a_document_filed_under_a_part_number_needs_no_guessing(tmp_path) -> None:
    """Filing a document under an MPN *is* the statement that it belongs to
    that part. Everything else in the resolver is inference about a filename
    somebody else chose."""
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    (proj / "datasheets" / "NRF9151-LACA-R").mkdir(parents=True)
    (proj / "datasheets" / "NRF9151-LACA-R" / "whatever-they-called-it.pdf").write_bytes(b"%PDF")
    found = datasheet_files.resolve(proj, "NRF9151-LACA-R")
    assert found.ok and found.how == "folder"


def test_the_folder_matches_the_way_part_numbers_compare(tmp_path) -> None:
    """Case and punctuation are not load-bearing in a part number, so a folder
    someone typed by hand still resolves."""
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    (proj / "datasheets" / "nrf9151_laca_r").mkdir(parents=True)
    (proj / "datasheets" / "nrf9151_laca_r" / "ds.pdf").write_bytes(b"%PDF")
    assert datasheet_files.resolve(proj, "NRF9151-LACA-R").how == "folder"


def test_two_documents_of_different_kinds_rank_rather_than_refuse(tmp_path) -> None:
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    d = proj / "datasheets" / "PART1"
    d.mkdir(parents=True)
    (d / "PART1_Errata_v1.pdf").write_bytes(b"%PDF")
    (d / "PART1_Datasheet_v2.pdf").write_bytes(b"%PDF")
    found = datasheet_files.resolve(proj, "PART1")
    assert found.ok and found.path.name == "PART1_Datasheet_v2.pdf"


def test_two_revisions_of_one_document_refuse_and_name_both(tmp_path) -> None:
    """Picking between revisions is picking a pinout."""
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    d = proj / "datasheets" / "PART1"
    d.mkdir(parents=True)
    (d / "PART1_Datasheet_v1.pdf").write_bytes(b"%PDF")
    (d / "PART1_Datasheet_v2.pdf").write_bytes(b"%PDF")
    found = datasheet_files.resolve(proj, "PART1")
    assert not found.ok and len(found.candidates) == 2


def test_a_folder_document_is_not_offered_to_a_different_part(tmp_path) -> None:
    """The prefix pass is why this matters: PART1 and PART2 share a stem, and a
    document filed under one must not become a candidate for the other."""
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    (proj / "datasheets" / "PART100").mkdir(parents=True)
    (proj / "datasheets" / "PART100" / "PART1_family.pdf").write_bytes(b"%PDF")
    assert not datasheet_files.resolve(proj, "PART200-XYZ").ok


def test_a_family_datasheet_still_resolves_from_the_top_level(tmp_path) -> None:
    """Folders do not take over storage. A datasheet covering three parts filed
    under one of them would be a lie; a copy in each would be the same 13 MB
    three times."""
    from blpl.agent.tools import datasheet_files

    proj = tmp_path / "p"
    sheets = proj / "datasheets"
    sheets.mkdir(parents=True)
    (sheets / "nRF54L15_nRF54L10_nRF54L05_Datasheet_v1.0.pdf").write_bytes(b"%PDF")
    for part in ("NRF54L15-QFAA-R", "NRF54L10-QFAA-R", "NRF54L05-QFAA-R"):
        assert datasheet_files.resolve(proj, part).how == "family", part


# ---------------------------------------------------------------------------
# Listing and reading directories
#
# Regression trio from a live turn that burned its whole context window: the
# user dropped eight datasheets into datasheets/rf-dividers-switches/, the
# listing showed none of them, read_project_file insisted the directory was
# "not in this project", and nothing stopped the agent retrying the same call.
# ---------------------------------------------------------------------------


def test_datasheets_in_subdirectories_are_listed(project) -> None:
    """A per-MPN subdirectory is the convention, so its files must be visible."""
    (project / "datasheets" / "rf-dividers").mkdir(parents=True)
    (project / "datasheets" / "rf-dividers" / "BD0926.pdf").write_bytes(b"%PDF")
    (project / "datasheets" / "top.pdf").write_bytes(b"%PDF")
    # Sidecars beside the PDF are what the extraction stages actually consume.
    (project / "datasheets" / "top.txt").write_text("pin 1 VDD", encoding="utf-8")

    out = json.loads(_call(_ctx(project), "list_project_files").content)
    assert "rf-dividers/BD0926.pdf" in out["datasheets"]
    assert "top.pdf" in out["datasheets"]
    assert "top.txt" in out["datasheets"]


def test_reading_a_directory_names_what_is_inside(project) -> None:
    (project / "datasheets" / "rf-dividers").mkdir(parents=True)
    (project / "datasheets" / "rf-dividers" / "BD0926.pdf").write_bytes(b"%PDF")

    r = _call(_ctx(project), "read_project_file", path="datasheets/rf-dividers")
    assert r.is_error
    assert "is a directory" in r.content
    assert "BD0926.pdf" in r.content  # the error carries the answer
    assert "in this project" not in r.content  # never claim it is absent


def test_the_same_failing_call_is_stopped(project) -> None:
    """Identical arguments failing identically must not loop forever."""

    async def approve(_request):
        return True

    ex = ToolExecutor(default_tools(), _ctx(project), approve=approve)

    def read(path: str):
        return asyncio.run(
            ex(ToolUseBlock(id="t1", name="read_project_file", input={"path": path}))
        )

    seen = [read("datasheets/nope.pdf") for _ in range(5)]
    assert all(r.is_error for r in seen)
    assert "already failed" not in seen[0].content
    assert "already failed" in seen[-1].content
    # One path's failures must not gag a different one.
    assert "already failed" not in read("datasheets/else.pdf").content


# ---------------------------------------------------------------------------
# Validating a document before proposing it
# ---------------------------------------------------------------------------


def test_doctor_and_stage0_are_available_to_the_assistant(project) -> None:
    """Instructions the model cannot act on are worse than none.

    The preamble tells it to check a document before proposing it. That is only
    true if the tools exist — the same trap as telling it to link a pinout file
    nothing reads."""
    names = {t.name for t in default_tools()}
    assert {"run_doctor", "check_stage0", "pinout_section"} <= names


def test_doctor_reports_errors_and_warnings_apart(project) -> None:
    (project / "board.md").write_text(
        "# B\n\n## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n"
        "| U1 | X | Package_BGA:does_not_exist |\n",
        encoding="utf-8",
    )
    out = json.loads(_call(_ctx(project), "run_doctor").content)
    assert set(out) >= {"errors", "warnings", "counts", "ready_for_stage0"}
    assert out["counts"]["errors"] == len(out["errors"])
    assert out["ready_for_stage0"] is (out["counts"]["errors"] == 0)


def test_a_tbd_footprint_raises_nothing_which_is_why_it_is_banned(project) -> None:
    """The reason the instruction changed.

    Doctor's footprint rule skips any cell without a ':' in it, so `TBD` there is
    not a question the pipeline ever asks — Stage 5 substitutes a placeholder and
    the board routes with the wrong copper."""
    (project / "board.md").write_text(
        "# B\n\n## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U1 | X | TBD |\n",
        encoding="utf-8",
    )
    out = json.loads(_call(_ctx(project), "run_doctor").content)
    assert not [f for f in out["errors"] if f.get("code") == "DOC-010"]


def test_check_stage0_reports_without_writing(project) -> None:
    before = sorted(p.name for p in (project / ".pipeline").iterdir())
    (project / "board.md").write_text(
        "# B\n\n## U1 — pinout\n\n| Pin | Signal |\n|---|---|\n| 1 | GND |\n| 2 | VDD |\n",
        encoding="utf-8",
    )
    out = json.loads(_call(_ctx(project), "check_stage0").content)
    assert out["total_pins"] == 2
    assert {"refdes": "U1", "pins": 2} in out["connectors"]
    # The point of a check is that it changes nothing.
    assert sorted(p.name for p in (project / ".pipeline").iterdir()) == before


def test_a_part_in_the_component_library_can_still_be_rendered(project) -> None:
    """Codex, PR #14. The advice could not be taken.

    extract_datasheet_specs returns early with a stored payload for a part
    already in the user's library and never writes a project-local extraction —
    so pinout_section found nothing and said to run extraction, which returned
    the same stored payload again."""
    from app.agent import ToolExecutor, default_tools

    class Lib:
        def get(self, mpn):
            return {"pinout": {"data": [{"numbers": ["1", "2"], "name": "VDD"}]}} if mpn == "X1" else None

        def put(self, mpn, payload):
            pass

    ctx = _ctx(project)
    ctx.library = Lib()

    async def approve(_r):
        return True

    ex = ToolExecutor(default_tools(), ctx, approve=approve)
    r = asyncio.run(ex(ToolUseBlock(id="t", name="pinout_section", input={"refdes": "U1", "mpn": "X1"})))
    assert not r.is_error, r.content
    assert "| 1 | VDD" in r.content and "| 2 | VDD" in r.content


def test_suggest_footprint_answers_with_the_set_not_a_favourite(project) -> None:
    """A BOM names a package and the pipeline needs a land pattern, and the gap
    is not closable by resemblance. Choosing is a decision with copper
    consequences, so this returns the whole set and what would narrow it."""
    fp = project / "libraries" / "footprints" / "Package_DFN_QFN.pretty"
    fp.mkdir(parents=True)
    for ep in ("2.5x2.5", "2.6x2.6"):
        (fp / f"QFN-24-1EP_4x4mm_P0.5mm_EP{ep}mm.kicad_mod").write_text("(footprint)")

    out = json.loads(
        _call(_ctx(project), "suggest_footprint", package="QFN-24-1EP_4x4mm_P0.5mm").content
    )
    assert out["outcome"] == "several"
    # The project's own come first, and the stock library's are here too — the
    # count is not pinned because it is a property of whatever kicad-footprints
    # is checked out, and the point is that no root is skipped.
    assert out["candidates"][0].startswith("Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.")
    assert len(out["candidates"]) > 2
    assert "exposed pad (from the datasheet, not the BOM)" in out["would_narrow_it"]
    assert out["read_as"]["pins"] == 24 and out["read_as"]["body_mm"] == [4.0, 4.0]


def test_suggest_footprint_says_when_nothing_fits(project) -> None:
    out = json.loads(_call(_ctx(project), "suggest_footprint", package="Module_25Pin").content)
    assert out["outcome"] == "none"
    assert not out["candidates"]
    assert "kicad-footprint-generator" in out["note"]


def test_an_edit_built_without_reading_is_refused(project) -> None:
    """The failure this exists to stop, and it is not hypothetical.

    A model with the file in its context from an earlier turn can compose a
    whole-file rewrite without re-reading. base_sha used to fall back to the
    live file's hash when nothing had been read, so apply_proposal compared the
    file against itself, found no conflict, and wrote a rewrite built from a
    snapshot several edits old. Every check passed; the checks were asking the
    wrong question.

    That cost a design document fourteen footprint references, four connector
    rows, and a part silently reverted to a different orderable variant — in an
    edit that reported success.
    """
    ctx = _ctx(project)
    # No read_project_file call. This is the whole point.
    res = _call(
        ctx, "propose_file_edit", path="overview.md",
        new_content="# Board\n\nRewritten from memory.\n", rationale="from memory",
    )

    assert res.is_error and "has not been read in this turn" in res.content
    assert ProposalStore(project).pending() == []
    assert (project / "overview.md").read_text() == "# Board\n\nOne connector.\n"


def test_an_edit_is_refused_when_the_file_moved_after_the_read(project) -> None:
    """Read early in a long turn, changed since: the edit is anchored to content
    that no longer exists. Caught at propose rather than at accept, so the model
    is told while it can still act and the stale proposal never exists to be
    accepted by mistake."""
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    (project / "overview.md").write_text("# Board\n\nSomeone else got here first.\n")

    res = _call(
        ctx, "propose_file_edit", path="overview.md",
        new_content="# Board v2\n", rationale="stale base",
    )

    assert res.is_error and "changed after you read it" in res.content
    assert ProposalStore(project).pending() == []


def test_creating_a_new_file_still_needs_no_prior_read(project) -> None:
    """There is nothing to read, so requiring a read would make creation
    impossible. base_sha is None here and apply_proposal already understands
    that as "must not exist yet"."""
    ctx = _ctx(project)
    res = _call(
        ctx, "propose_file_edit", path="new-board.md",
        new_content="# New\n", rationale="create",
    )

    assert not res.is_error
    pending = ProposalStore(project).pending()
    assert len(pending) == 1 and pending[0].base_sha is None


def test_an_edit_is_refused_when_the_file_was_deleted_after_the_read(project) -> None:
    """Read it, delete it, propose the old content back.

    The first version of this check let this through as a creation: with the
    file gone, base_sha became None, and apply_proposal only tests staleness for
    a path that still exists — so the edit sailed through and recreated a
    document somebody had deleted, from a copy predating the deletion, with no
    conflict reported because nothing conflicted.

    Deleting a file is a decision. Undoing it silently is not this tool's to
    make.
    """
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    (project / "overview.md").unlink()

    res = _call(
        ctx, "propose_file_edit", path="overview.md",
        new_content="# Board\n\nOne connector.\n", rationale="put it back",
    )

    assert res.is_error and "deleted or replaced" in res.content
    assert ProposalStore(project).pending() == []
    assert not (project / "overview.md").exists()


def test_an_edit_is_refused_when_a_directory_took_the_files_place(project) -> None:
    """Same branch, and it would otherwise fail mid-write inside apply_proposal
    rather than being caught while the model can still react."""
    ctx = _ctx(project)
    _call(ctx, "read_project_file", path="overview.md")
    (project / "overview.md").unlink()
    (project / "overview.md").mkdir()

    res = _call(
        ctx, "propose_file_edit", path="overview.md", new_content="# x\n", rationale="r"
    )

    assert res.is_error and "deleted or replaced" in res.content
    assert ProposalStore(project).pending() == []


def test_a_directory_in_the_way_of_a_new_file_is_refused_too(project) -> None:
    """Never read, so there is no base to preserve — but still not creatable."""
    ctx = _ctx(project)
    (project / "notes.md").mkdir()

    res = _call(ctx, "propose_file_edit", path="notes.md", new_content="# x\n", rationale="r")

    assert res.is_error and "is not a file" in res.content
