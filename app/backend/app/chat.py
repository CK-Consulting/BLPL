"""In-app design chat: streamed turns, project-scoped tools, and file proposals.

This is the piece that closes the loop the workbench was missing. Design
conversations used to happen in a separate app and the resulting markdown got
hand-copied into a project directory — which is tedious once, and corrosive
across the dozen iterations a real board takes. Here the conversation happens
next to the project, reads its files and artifacts directly, and writes changes
back through a review step.

Three things live here.

**Sessions.** A turn runs in-process rather than as a subprocess, because chat
needs sub-second first tokens and, later, approval round-trips mid-turn — both
impossible across a pipe. The fan-out mirrors ``runs._LiveRun`` exactly
(synchronous publish into a buffer plus every subscriber queue, sentinel to
close): reconnecting mid-answer replays from the top and then tails, so a
browser refresh costs nothing. Unlike a run, a turn is *not* durable — if the
server dies mid-answer the answer is gone. That is a deliberate asymmetry: the
user message is persisted the moment it arrives, so nothing the human typed is
ever lost, and a dropped reply is one retry rather than a corrupted artifact.

**Tools.** Read-only ones (files, artifacts) plus one writer that does not
write. Every path goes through ``FilesystemSandbox`` — this is that module's
first runtime consumer, and the reason it was built.

**Proposals.** The assistant never writes a user's file directly. It proposes,
recording the file's hash at read time; the user sees a diff and accepts. The
accept re-checks that hash and refuses a stale one instead of merging.

That last decision is the important one, and it is not ceremony. The editor
saves whole files and has no way to learn that something changed underneath it,
so a direct agent write lands in a race it can only lose: the user's next Save
would silently restore the old content, or the agent's write would silently
discard whatever the user was typing. A proposal costs one click and turns that
race into a diff you can read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from blpl.core import llm_chat
from blpl.core.llm_chat import (
    ChatEvent,
    Done,
    Endpoint,
    Msg,
    TextBlock,
    ToolDecl,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    build_chat_adapter,
    run_tool_loop,
)

from .conversations import Conversation
from .references import FilesystemSandbox, ReferencePolicyError

# The design-markdown contract Stage 0 actually parses — table shapes, refdes
# anchoring, the pitfalls that have broken real boards. It ships with the package
# for exactly this kind of consumer, so the assistant is briefed from the same
# source a Claude Code session installed into the project would use, instead of a
# second copy of the rules drifting out of sync with the parser.
_SKILL_PATH = Path(llm_chat.__file__).resolve().parent.parent / "skills" / "hardware-design" / "SKILL.md"

_PREAMBLE = """\
You are BLPL's hardware design assistant, working inside the BLPL workbench on \
one project at a time. BLPL compiles Markdown design documents into a KiCad \
project through a deterministic 9-stage pipeline.

How you work here:

- You can read the project's design markdown and its generated pipeline \
artifacts with your tools. Read before you assert — the artifacts say what the \
pipeline actually produced, and that is the ground truth about this board.
- You cannot write files. To change one, call propose_file_edit; the user sees \
your change as a diff and accepts or rejects it. Propose the complete new file \
content, not a fragment. Say in the rationale what you changed and why.
- When the user is deciding something — which part, which pinout, whether a \
warning matters — walk them through the evidence you actually read, quoting the \
specific line or row. Do not guess an MPN or a footprint; if the design does not \
say and you cannot read it, ask.
- Match the design documents' existing conventions rather than imposing new ones.

The Markdown contract below is what the parser enforces. Design documents you \
write or edit must follow it exactly, because anything outside it is silently \
ignored by Stage 0 or lands in the wrong place.
"""


def build_system_prompt() -> str:
    """Preamble plus the packaged hardware-design skill, when it is present."""
    try:
        skill = _SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        # A packaging accident should cost the assistant its contract briefing,
        # not the ability to hold a conversation.
        return _PREAMBLE
    return f"{_PREAMBLE}\n\n---\n\n{skill}"


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


class ProposalError(RuntimeError):
    """A proposal could not be created or applied. Carries a message meant for
    the user, not a stack trace."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Proposal:
    id: str
    path: str            # project-relative
    new_content: str
    rationale: str
    base_sha: str | None  # None means "this file does not exist yet"
    status: str = "pending"   # pending | accepted | rejected | stale
    created_at: str = ""
    conversation: str = ""

    def to_dict(self, *, include_content: bool = True) -> dict:
        out = {
            "id": self.id,
            "path": self.path,
            "rationale": self.rationale,
            "base_sha": self.base_sha,
            "status": self.status,
            "created_at": self.created_at,
            "conversation": self.conversation,
            "creates_file": self.base_sha is None,
        }
        if include_content:
            out["new_content"] = self.new_content
        return out


class ProposalStore:
    """Pending file edits, on disk under ``.blpl/proposals/``.

    On disk rather than in memory so a proposal survives the restart that kills
    its turn: the answer may be gone, but the edit it offered is still reviewable.
    """

    def __init__(self, project_dir: Path):
        self.dir = Path(project_dir) / ".blpl" / "proposals"

    def _path(self, proposal_id: str) -> Path:
        if not proposal_id.startswith("prop_") or "/" in proposal_id or "\\" in proposal_id:
            raise ProposalError(f"invalid proposal id {proposal_id!r}")
        return self.dir / f"{proposal_id}.json"

    def create(
        self, *, path: str, new_content: str, rationale: str, base_sha: str | None, conversation: str = ""
    ) -> Proposal:
        proposal = Proposal(
            id="prop_" + uuid.uuid4().hex[:12],
            path=path,
            new_content=new_content,
            rationale=rationale,
            base_sha=base_sha,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            conversation=conversation,
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(proposal.id).write_text(json.dumps(proposal.to_dict(), indent=2), encoding="utf-8")
        return proposal

    def get(self, proposal_id: str) -> Proposal | None:
        p = self._path(proposal_id)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return Proposal(
            id=data["id"],
            path=data["path"],
            new_content=data.get("new_content", ""),
            rationale=data.get("rationale", ""),
            base_sha=data.get("base_sha"),
            status=data.get("status", "pending"),
            created_at=data.get("created_at", ""),
            conversation=data.get("conversation", ""),
        )

    def set_status(self, proposal: Proposal, status: str) -> Proposal:
        proposal.status = status
        self._path(proposal.id).write_text(json.dumps(proposal.to_dict(), indent=2), encoding="utf-8")
        return proposal

    def pending(self) -> list[Proposal]:
        if not self.dir.is_dir():
            return []
        out = []
        for f in sorted(self.dir.glob("prop_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status") == "pending":
                p = self.get(data["id"])
                if p:
                    out.append(p)
        return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# What a design document may be. Same suffix rule the file editor enforces, so
# the assistant cannot propose a file the user has no way to open.
_EDITABLE_SUFFIXES = {".md", ".markdown", ".yaml", ".yml"}


@dataclass
class ToolContext:
    project_id: str
    project_dir: Path
    sandbox: FilesystemSandbox
    proposals: ProposalStore
    conversation: str = ""
    # Files this turn has read, path → sha at read time. A proposal's base_sha
    # comes from here rather than from a fresh stat: the hash must describe the
    # bytes the model actually reasoned about, or the staleness check is checking
    # the wrong thing.
    read_shas: dict[str, str] = field(default_factory=dict)


def tool_declarations() -> list[ToolDecl]:
    return [
        ToolDecl(
            name="list_project_files",
            description=(
                "List the project's design documents (Markdown and YAML in the project root) "
                "and the generated pipeline artifacts in .pipeline/. Start here when you do "
                "not know what the project contains."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolDecl(
            name="read_project_file",
            description=(
                "Read one design document from the project root by filename, e.g. "
                "'overview.md' or 'project.yaml'."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Filename in the project root."}},
                "required": ["path"],
            },
        ),
        ToolDecl(
            name="read_pipeline_artifact",
            description=(
                "Read a generated artifact from .pipeline/ — design_artifact.deterministic.json, "
                "bom.json, nets.json, coverage_report.json, review_report.json, validation_report.json, "
                "gaps.md, and so on. This is what the pipeline actually produced; prefer it over "
                "assumptions when diagnosing. Large artifacts are truncated, and the reply says so."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Artifact filename inside .pipeline/."}},
                "required": ["name"],
            },
        ),
        ToolDecl(
            name="propose_file_edit",
            description=(
                "Propose new content for a design document. The user reviews it as a diff and "
                "accepts or rejects — nothing is written until they do. Supply the COMPLETE new "
                "file content. Read the file first unless you are creating it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename in the project root (.md/.yaml)."},
                    "new_content": {"type": "string", "description": "The complete new file content."},
                    "rationale": {"type": "string", "description": "What changed and why, in one or two sentences."},
                },
                "required": ["path", "new_content", "rationale"],
            },
        ),
    ]


# One artifact should inform an answer, not consume the context window. A
# truncated read that says it was truncated beats a silent half-read.
_MAX_ARTIFACT_CHARS = 60_000


def _elide(text: str, limit: int = _MAX_ARTIFACT_CHARS) -> str:
    """Keep both ends and cut the middle.

    Head-only truncation was actively misleading: nets.json is ~100k characters
    of net list with its findings at one end, so a prefix read produced an
    assistant that could see every net and none of the problems. Both ends
    survive now, and the elision says how much is missing so nobody reasons
    from a hole they cannot see.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return (
        text[:head]
        + f"\n\n… {len(text) - limit} characters elided from the middle "
        f"({len(text)} total) …\n\n"
        + text[-tail:]
    )


def _safe_project_file(ctx: ToolContext, name: str) -> Path:
    """Resolve a design-document name, refusing anything that leaves the root.

    Same three rules as the editor endpoint — bare filename, editable suffix,
    parent is the project root — and then the sandbox has the final say.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ProposalError(f"{name!r} must be a bare filename in the project root")
    target = (ctx.project_dir / name).resolve()
    if target.parent != ctx.project_dir.resolve():
        raise ProposalError(f"{name!r} must be a bare filename in the project root")
    if target.suffix.lower() not in _EDITABLE_SUFFIXES:
        raise ProposalError(
            f"{name!r} is not an editable design document "
            f"({', '.join(sorted(_EDITABLE_SUFFIXES))})"
        )
    return target


def make_executor(ctx: ToolContext) -> Callable:
    """Bind the tool implementations to one project and turn.

    Failures come back as error *results*, never exceptions: a bad path or a
    missing file is something the model should see and correct, and raising here
    would end the turn instead of the conversation continuing.
    """

    async def execute(call: ToolUseBlock) -> ToolResultBlock:
        def err(msg: str) -> ToolResultBlock:
            return ToolResultBlock(tool_use_id=call.id, content=msg, is_error=True)

        def ok(msg: str) -> ToolResultBlock:
            return ToolResultBlock(tool_use_id=call.id, content=msg)

        args = call.input or {}
        if "__malformed_arguments__" in args:
            return err("tool arguments were not valid JSON; call the tool again with complete arguments")

        try:
            if call.name == "list_project_files":
                docs = sorted(
                    f.name
                    for f in ctx.project_dir.iterdir()
                    if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in _EDITABLE_SUFFIXES
                )
                pipeline = ctx.project_dir / ".pipeline"
                arts = sorted(f.name for f in pipeline.iterdir() if f.is_file()) if pipeline.is_dir() else []
                return ok(
                    json.dumps({"design_documents": docs, "pipeline_artifacts": arts}, indent=2)
                )

            if call.name == "read_project_file":
                target = _safe_project_file(ctx, str(args.get("path", "")))
                ctx.sandbox.check_read(target)
                if not target.is_file():
                    return err(f"no file {target.name!r} in this project")
                text = target.read_text(encoding="utf-8", errors="replace")
                ctx.read_shas[target.name] = _sha(text)
                return ok(text)

            if call.name == "read_pipeline_artifact":
                name = str(args.get("name", ""))
                if not name or "/" in name or "\\" in name:
                    return err("artifact name must be a bare filename inside .pipeline/")
                target = (ctx.project_dir / ".pipeline" / name).resolve()
                if not target.is_relative_to((ctx.project_dir / ".pipeline").resolve()):
                    return err("artifact name must be a bare filename inside .pipeline/")
                ctx.sandbox.check_read(target)
                if not target.is_file():
                    return err(f"no artifact {name!r} — has that stage run yet?")
                text = target.read_text(encoding="utf-8", errors="replace")
                return ok(_elide(text))

            if call.name == "propose_file_edit":
                target = _safe_project_file(ctx, str(args.get("path", "")))
                new_content = args.get("new_content")
                if not isinstance(new_content, str):
                    return err("new_content must be the complete new file content, as a string")
                ctx.sandbox.check_write(target)

                exists = target.is_file()
                current = target.read_text(encoding="utf-8", errors="replace") if exists else None
                if exists and current is not None and _sha(current) == _sha(new_content):
                    return err(
                        f"{target.name} already has exactly this content — nothing to propose"
                    )
                # Prefer the hash from when this turn read the file; fall back to
                # the file as it is now for a blind edit.
                base_sha = ctx.read_shas.get(target.name) or (_sha(current) if current is not None else None)
                proposal = ctx.proposals.create(
                    path=target.name,
                    new_content=new_content,
                    rationale=str(args.get("rationale", "")),
                    base_sha=base_sha,
                    conversation=ctx.conversation,
                )
                verb = "create" if not exists else "update"
                return ok(
                    f"Proposed to {verb} {target.name} (proposal {proposal.id}). "
                    "The user must accept it before anything is written; do not assume it is applied."
                )

            return err(f"unknown tool {call.name!r}")
        except ProposalError as exc:
            return err(str(exc))
        except ReferencePolicyError as exc:
            # A denial is information the model should act on, not a crash.
            return err(f"refused by the project sandbox: {exc}")
        except OSError as exc:
            return err(f"filesystem error: {exc}")

    return execute


def apply_proposal(
    proposal: Proposal, project_dir: Path, sandbox: FilesystemSandbox
) -> tuple[bool, str]:
    """Write an accepted proposal, or refuse with a reason.

    The staleness check is the whole point: between proposing and accepting, the
    user may have saved their own edit to the same file. Overwriting that would
    destroy work nobody agreed to lose, and a three-way merge here would be
    guessing. Refusing sends the assistant back to re-read — cheap, and correct.
    """
    target = (Path(project_dir) / proposal.path).resolve()
    if target.parent != Path(project_dir).resolve():
        return False, f"invalid proposal path {proposal.path!r}"

    try:
        sandbox.check_write(target)
    except ReferencePolicyError as exc:
        return False, f"refused by the project sandbox: {exc}"

    exists = target.is_file()
    if exists:
        current = target.read_text(encoding="utf-8", errors="replace")
        if proposal.base_sha is None:
            return False, (
                f"{proposal.path} was created after this proposal was made — "
                "re-read the file and propose again"
            )
        if _sha(current) != proposal.base_sha:
            return False, (
                f"{proposal.path} changed since this edit was proposed — "
                "the proposal is stale. Re-read the file and propose again."
            )
    elif proposal.base_sha is not None:
        return False, f"{proposal.path} no longer exists — re-read and propose again"

    target.write_text(proposal.new_content, encoding="utf-8")
    return True, f"wrote {proposal.path}"


# ---------------------------------------------------------------------------
# Conversation history ⇄ chat messages
# ---------------------------------------------------------------------------


def _blocks_to_json(msg: Msg) -> list[dict]:
    out: list[dict] = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            out.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": b.content,
                    "is_error": b.is_error,
                }
            )
    return out


def _blocks_from_json(raw: Iterable[dict]) -> list:
    blocks: list = []
    for b in raw or []:
        kind = b.get("type")
        if kind == "text":
            blocks.append(TextBlock(b.get("text", "")))
        elif kind == "tool_use":
            blocks.append(ToolUseBlock(id=b.get("id", ""), name=b.get("name", ""), input=b.get("input") or {}))
        elif kind == "tool_result":
            blocks.append(
                ToolResultBlock(
                    tool_use_id=b.get("tool_use_id", ""),
                    content=b.get("content", ""),
                    is_error=bool(b.get("is_error")),
                )
            )
    return blocks


def history_to_messages(events: Iterable[dict]) -> list[Msg]:
    """Rebuild the chat history from the conversation's JSONL.

    Tool calls and their results are replayed too, not just prose. Dropping them
    would leave assistant turns referring to tool calls the provider can no
    longer see — which providers reject outright, and which would in any case
    strip the evidence the conversation was reasoning from.
    """
    messages: list[Msg] = []
    for ev in events:
        role = ev.get("role")
        meta = ev.get("metadata") or {}
        blocks = _blocks_from_json(meta.get("blocks") or [])
        if role == "user":
            messages.append(Msg(role="user", content=blocks or [TextBlock(ev.get("content", ""))]))
        elif role == "assistant":
            messages.append(
                Msg(
                    role="assistant",
                    content=blocks or [TextBlock(ev.get("content", ""))],
                    raw_provider=meta.get("raw_provider"),
                    raw_content=meta.get("raw_content"),
                )
            )
        elif role == "tool_results":
            if blocks:
                messages.append(Msg(role="user", content=blocks))
        # Any other role (e.g. the "error" marker conversations.py emits for a
        # corrupt line) is skipped: it is a record for humans, not context.
    return messages


def persist_messages(conv: Conversation, messages: Iterable[Msg], *, model: str, usage: dict | None) -> None:
    for msg in messages:
        blocks = _blocks_to_json(msg)
        if msg.role == "assistant":
            meta: dict = {"blocks": blocks, "model": model}
            if msg.raw_provider:
                meta["raw_provider"] = msg.raw_provider
                meta["raw_content"] = msg.raw_content
            if usage:
                meta["usage"] = usage
            conv.append("assistant", msg.text, meta)
        else:
            conv.append("tool_results", "", {"blocks": blocks})


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class _LiveTurn:
    """One in-flight turn and everyone watching it.

    ``publish`` appends to the buffer and every subscriber queue in one
    synchronous step — no await in between — so a subscriber that snapshots the
    buffer and then attaches its queue can neither miss an event nor see one
    twice. Same invariant as ``runs._LiveRun``, and it is the reason a mid-answer
    refresh is seamless.
    """

    def __init__(self, turn_id: str, conversation: str):
        self.turn_id = turn_id
        self.conversation = conversation
        self.events: list[dict] = []
        self.queues: set[asyncio.Queue] = set()
        self.done = False
        self.task: asyncio.Task | None = None

    def publish(self, event: dict) -> None:
        self.events.append(event)
        for q in self.queues:
            q.put_nowait(event)

    def finish(self) -> None:
        self.done = True
        for q in self.queues:
            q.put_nowait(None)


@dataclass
class TurnRequest:
    project_id: str
    project_dir: Path
    conversation: Conversation
    endpoint: Endpoint
    sandbox: FilesystemSandbox
    usage_ledger: Path | None = None


# Finished turns are kept briefly so a reader that arrives late still gets the
# whole answer. Without this, a short cached reply can finish before the browser
# opens its stream, and the user watches an empty panel while the text sits in
# a buffer nobody can reach any more.
_RETAINED_TURNS = 32


class ChatSessionManager:
    """Runs turns and fans their events out to any number of readers."""

    def __init__(self) -> None:
        self._turns: dict[str, _LiveTurn] = {}

    def active_for(self, conversation_filename: str) -> str | None:
        for turn in self._turns.values():
            if turn.conversation == conversation_filename and not turn.done:
                return turn.turn_id
        return None

    def _prune(self) -> None:
        finished = [t for t in self._turns.values() if t.done]
        for turn in finished[: max(0, len(finished) - _RETAINED_TURNS)]:
            self._turns.pop(turn.turn_id, None)

    def start(self, req: TurnRequest) -> str:
        """Launch a turn. The caller has already persisted the user's message."""
        existing = self.active_for(req.conversation.path.name)
        if existing:
            raise ProposalError(
                f"this conversation already has a turn in flight ({existing}); "
                "wait for it to finish"
            )
        turn_id = "turn_" + uuid.uuid4().hex[:12]
        live = _LiveTurn(turn_id, req.conversation.path.name)
        self._turns[turn_id] = live
        self._prune()
        live.task = asyncio.get_running_loop().create_task(self._run(live, req))
        return turn_id

    async def _run(self, live: _LiveTurn, req: TurnRequest) -> None:
        ctx = ToolContext(
            project_id=req.project_id,
            project_dir=req.project_dir,
            sandbox=req.sandbox,
            proposals=ProposalStore(req.project_dir),
            conversation=req.conversation.path.name,
        )
        live.publish({"type": "start", "turn_id": live.turn_id, "model": req.endpoint.model,
                      "endpoint": req.endpoint.name})
        try:
            messages = history_to_messages(req.conversation.read_all())
            adapter = build_chat_adapter(req.endpoint)

            def on_event(event: ChatEvent) -> None:
                payload = event.to_dict()
                # A proposal is the one tool result the UI must render as a
                # reviewable card rather than a log line, so it is promoted to
                # its own event with the proposal attached.
                if isinstance(event, llm_chat.ToolResult) and event.name == "propose_file_edit":
                    pid = _proposal_id_in(event.content)
                    if pid:
                        proposal = ctx.proposals.get(pid)
                        if proposal:
                            live.publish({"type": "proposal", "proposal": proposal.to_dict()})
                if isinstance(event, Done):
                    # The loop may take several provider turns. Mark the seam so
                    # the live transcript breaks where the persisted one will.
                    live.publish({"type": "segment"})
                else:
                    live.publish(payload)

            result = await run_tool_loop(
                adapter,
                messages,
                system=build_system_prompt(),
                tools=tool_declarations(),
                execute=make_executor(ctx),
                on_event=on_event,
            )

            usage = _sum_usage(result.usage)
            persist_messages(req.conversation, result.new_messages, model=req.endpoint.model, usage=usage)
            if req.usage_ledger is not None:
                for u in result.usage:
                    llm_chat.append_usage(
                        req.usage_ledger, u,
                        context={"project": req.project_id, "conversation": ctx.conversation},
                    )
            live.publish(
                {
                    "type": "done",
                    "turn_id": live.turn_id,
                    "stop_reason": result.stop_reason,
                    "iterations": result.iterations,
                    "usage": usage,
                }
            )
        except Exception as exc:  # noqa: BLE001 — any failure must reach the user
            # The turn is lost either way; what must not be lost is the reason.
            # It is recorded in the conversation so it survives the page, and
            # published so whoever is watching sees it now.
            detail = f"{type(exc).__name__}: {exc}"
            try:
                req.conversation.append("error", detail, {"turn_id": live.turn_id})
            except OSError:
                pass
            live.publish({"type": "error", "turn_id": live.turn_id, "detail": detail})
        finally:
            live.finish()

    async def stream(self, turn_id: str):
        """Replay this turn from the top, then follow it live.

        Attaching the queue, snapshotting the buffer, and reading ``done`` all
        happen without an await between them, so no publish can slip into the
        gap — the snapshot plus the queue is exactly the event stream, with
        nothing missed and nothing seen twice.
        """
        live = self._turns.get(turn_id)
        if live is None:
            # Aged out, or never existed: the conversation JSONL is the record.
            yield {"type": "done", "turn_id": turn_id, "stop_reason": "not_live"}
            return

        q: asyncio.Queue = asyncio.Queue()
        live.queues.add(q)
        snapshot = list(live.events)
        finished = live.done
        try:
            for event in snapshot:
                yield event
            if finished:
                return
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            live.queues.discard(q)


def _proposal_id_in(text: str) -> str | None:
    for word in text.replace(")", " ").replace("(", " ").split():
        if word.startswith("prop_"):
            return word
    return None


def _sum_usage(usages: list[Usage]) -> dict:
    return {
        "input_tokens": sum(u.input_tokens for u in usages),
        "output_tokens": sum(u.output_tokens for u in usages),
        "cache_read_tokens": sum(u.cache_read_tokens for u in usages),
    }
