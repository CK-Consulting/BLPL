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
from dataclasses import dataclass, field, replace
from pathlib import Path

from blpl.core import llm_chat
from blpl.core.llm_chat import (
    ChatEvent,
    DocumentBlock,
    Done,
    Endpoint,
    ImageBlock,
    Msg,
    TextBlock,
    ToolDecl,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    build_chat_adapter,
    run_tool_loop,
)

from . import attachments as attachments_store
from .agent import ToolContext, ToolExecutor, default_tools
from .agent.toolspec import ApprovalRequest
from .conversations import Conversation
from .references import FilesystemSandbox

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

- You can read anything in the project directory with your tools — design \
markdown, sub-board directories, notes, generated pipeline artifacts. Reading \
is free and needs nobody's permission, so read before you assert: the artifacts \
say what the pipeline actually produced, and that is the ground truth about \
this board. Call list_project_files when you do not know what is there.
- One exception, and it is the user's instruction rather than a permission: \
files under `context-ignore/` are things they keep in the project but have \
declared irrelevant to the board. Do not read them, and do not reason from \
them, unless the user asks about one by name. Then read it like any other file.
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


# Provider failures that are about the provider's moment rather than the
# request. The SDK already retries these a couple of times inside the call; by
# the time one reaches here that budget is spent, and the honest thing is to
# tell the user it is worth asking again rather than presenting it as a fault
# in what they typed.
#
# 529 `overloaded_error` is the one worth naming: it means Anthropic is briefly
# at capacity. It carries no information about prompt size, and shortening the
# conversation does not affect it.
_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
_TRANSIENT_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
}


def is_transient(exc: BaseException) -> bool:
    """Whether asking the same question again could plausibly succeed.

    Duck-typed rather than caught by class: the provider SDKs are optional
    dependencies here, and this module must not import a vendor package just to
    decide how to phrase an error message.
    """
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _TRANSIENT_STATUS:
        return True
    # Some adapters flatten the provider's JSON into the message instead of
    # raising a typed error, so the wire vocabulary is the last resort.
    text = str(exc)
    return "overloaded_error" in text or "rate_limit_error" in text


def worth_another_endpoint(exc: BaseException) -> bool:
    """Whether a *different* endpoint could plausibly succeed where this failed.

    Broader than ``is_transient``, and for a different reason. Transient means
    "ask again"; this means "ask someone else". The case that prompted it: a
    model with no tool support answers 400 ``does not support tools`` — asking
    it again is pointless, and asking the next endpoint in the chain works
    immediately.

    Deliberately not a catch-all. A tool that raised, a refusal, or a bad
    request we constructed will fail identically everywhere, and retrying those
    across four providers would turn one clear error into four slow ones.
    """
    if is_transient(exc):
        return True
    text = str(exc).lower()
    return any(
        phrase in text
        for phrase in (
            "does not support tools",
            "does not support",
            "not supported",
            "unsupported",
            "no endpoints available",
            "model not found",
            "is not a multimodal model",
        )
    )


class TurnInFlight(RuntimeError):
    """A turn is already running for this conversation.

    Carries the turn id rather than only a sentence about it. The browser that
    hits this has almost always just lost its event stream — the turn it is
    being refused is *its own* — so the id is what turns a dead end into a
    reattach. Previously this was a ProposalError whose message happened to
    mention the id, which meant the only way to recover it was to parse English.
    """

    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        super().__init__(
            f"this conversation already has a turn in flight ({turn_id}); "
            "reattaching to it"
        )


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
#
# The tool set, its policies, and the executor now live in app/agent/ — chat is
# one caller of that platform, not its owner. What stays here is the proposal
# machinery the tools write into, and the hash rule they anchor to.
# ---------------------------------------------------------------------------


def sha_of(text: str) -> str:
    """Public alias of the content hash proposals are anchored to.

    Exported because the propose/read tools live in the agent registry now and
    both halves of the staleness check must compute it the same way.
    """
    return _sha(text)


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
        elif isinstance(b, (ImageBlock, DocumentBlock)):
            # Only reachable for media the *model* produced or a tool returned,
            # never for a user's attachment: those are written straight to the
            # conversation as reference blocks by the upload route, and reach
            # the provider through _blocks_from_json without passing here.
            #
            # There is no store id to write, and inlining the base64 is the one
            # thing this format exists to avoid (see app/attachments.py), so
            # what is persisted is the fact that it happened.
            out.append(
                {
                    "type": "text",
                    "text": f"[{b.media_type} generated in this turn; not stored]",
                }
            )
    return out


def _blocks_from_json(raw: Iterable[dict], attachments_dir: Path | None = None) -> list:
    blocks: list = []
    for b in raw or []:
        kind = b.get("type")
        if kind == "text":
            blocks.append(TextBlock(b.get("text", "")))
        elif kind in ("image", "document"):
            # No store to read from means this is a caller that only wants the
            # shape of the history (the UI). Skip rather than fabricate: an
            # empty ImageBlock would be sent to the provider as a broken image.
            if attachments_dir is None:
                continue
            data = attachments_store.read_b64(attachments_dir, b.get("attachment", ""))
            if data is None:
                # The reference outlived the bytes. Say so in the transcript
                # rather than dropping it silently — the assistant answering
                # "as you can see in the image" about an image it never
                # received is worse than being told the image is gone.
                blocks.append(
                    TextBlock(f"[attachment {b.get('name') or 'file'} is no longer available]")
                )
                continue
            if kind == "image":
                blocks.append(ImageBlock(data=data, media_type=b.get("media_type", "image/png")))
            else:
                blocks.append(
                    DocumentBlock(
                        data=data,
                        media_type=b.get("media_type", "application/pdf"),
                        # Providers show this to the model; a datasheet that
                        # arrives called "document 1" is worth less than one
                        # that arrives called "TPS62840.pdf".
                        filename=b.get("name") or None,
                    )
                )
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


def history_to_messages(
    events: Iterable[dict], attachments_dir: Path | None = None
) -> list[Msg]:
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
        blocks = _blocks_from_json(meta.get("blocks") or [], attachments_dir)
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
        # call_id → the future the executor is blocked on. A turn awaiting one
        # of these is idle, not stuck: it resumes the moment a human answers.
        self.approvals: dict[str, asyncio.Future] = {}

    def publish(self, event: dict) -> None:
        self.events.append(event)
        for q in self.queues:
            q.put_nowait(event)

    def finish(self) -> None:
        self.done = True
        # Anything still waiting for an answer will never get one now; resolving
        # them as denied is what lets the loop unwind instead of hanging.
        for fut in self.approvals.values():
            if not fut.done():
                fut.set_result(False)
        self.approvals.clear()
        for q in self.queues:
            q.put_nowait(None)


@dataclass
class TurnRequest:
    project_id: str
    project_dir: Path
    conversation: Conversation
    endpoint: Endpoint
    # The rest of the routed chain, in order. A chain that is only ever used
    # for its head is not a fallback chain — it is a list with decoration, and
    # that is what this was: an endpoint that could not serve the turn ended it
    # rather than passing it on to the next one that could.
    fallbacks: tuple[Endpoint, ...] = ()
    sandbox: FilesystemSandbox = None  # type: ignore[assignment]
    usage_ledger: Path | None = None
    creds: object | None = None
    # task name → endpoint, so a tool that needs a different model (datasheet
    # extraction needs one that can see) gets the routed one rather than this
    # conversation's.
    endpoints_for: Callable[[str], list[Endpoint]] | None = None
    record_tool_call: Callable[[dict], None] | None = None
    # Where the KiCad MCP server is, if the deploy has one.
    kicad_url: str | None = None
    # The conversations directory, so attachments referenced in the transcript
    # can be read back into the provider's message list.
    conversations_dir: Path | None = None


# Finished turns are kept briefly so a reader that arrives late still gets the
# whole answer. Without this, a short cached reply can finish before the browser
# opens its stream, and the user watches an empty panel while the text sits in
# a buffer nobody can reach any more.
_RETAINED_TURNS = 32

# How long a tool call may sit waiting for a human. Long enough that stepping
# away mid-review does not lose the turn; short enough that an abandoned
# session eventually unwinds instead of pinning a loop forever.
APPROVAL_TIMEOUT_SECONDS = 600

# How often an otherwise silent stream emits a keepalive. Comfortably under the
# 60s idle timeout that proxies, load balancers and tunnels commonly default to,
# and cheap enough that it costs nothing to be well inside it.
HEARTBEAT_SECONDS = 15


class ChatSessionManager:
    """Runs turns and fans their events out to any number of readers."""

    def __init__(self) -> None:
        self._turns: dict[str, _LiveTurn] = {}
        # conversation filename → tools approved in it. Held here rather than on
        # the executor because an executor lives for a single turn, which made
        # a once-per-session question fire on every message.
        #
        # Deliberately not persisted: consent is scoped to a running server, so
        # a restart asks again. That is a cheap question to answer once and the
        # honest default for something granting network access.
        self._approved: dict[str, set[str]] = {}

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
            raise TurnInFlight(existing)
        turn_id = "turn_" + uuid.uuid4().hex[:12]
        live = _LiveTurn(turn_id, req.conversation.path.name)
        self._turns[turn_id] = live
        self._prune()
        live.task = asyncio.get_running_loop().create_task(self._run(live, req))
        return turn_id

    async def _run(self, live: _LiveTurn, req: TurnRequest) -> None:
        from blpl.agent.kicad_happy import CredResolver

        ctx = ToolContext(
            project_id=req.project_id,
            project_dir=req.project_dir,
            sandbox=req.sandbox,
            conversation=req.conversation.path.name,
            creds=req.creds or CredResolver(),
            endpoints_for=req.endpoints_for or (lambda _t: []),
            on_progress=lambda m: live.publish({"type": "progress", "message": m}),
            # A tool whose whole effect is visual publishes straight to the
            # browser; highlighting the part under discussion beats describing
            # where to look.
            on_ui=lambda event: live.publish({"type": "ui", **event}),
        )
        live.publish({"type": "start", "turn_id": live.turn_id, "model": req.endpoint.model,
                      "endpoint": req.endpoint.name})
        try:
            messages = history_to_messages(
                req.conversation.read_all(),
                req.conversations_dir or req.conversation.path.parent,
            )
            executor = ToolExecutor(
                default_tools(req.kicad_url),
                ctx,
                approve=lambda request: self._ask(live, request),
                record=req.record_tool_call,
                session_approved=self._approved.setdefault(live.conversation, set()),
            )

            def on_event(event: ChatEvent) -> None:
                payload = event.to_dict()
                # A proposal is the one tool result the UI must render as a
                # reviewable card rather than a log line, so it is promoted to
                # its own event with the proposal attached.
                if isinstance(event, llm_chat.ToolResult) and event.name == "propose_file_edit":
                    pid = _proposal_id_in(event.content)
                    if pid:
                        proposal = ProposalStore(req.project_dir).get(pid)
                        if proposal:
                            live.publish({"type": "proposal", "proposal": proposal.to_dict()})
                if isinstance(event, Done):
                    # The loop may take several provider turns. Mark the seam so
                    # the live transcript breaks where the persisted one will.
                    live.publish({"type": "segment"})
                else:
                    live.publish(payload)

            # Walk the routed chain. Only the head was ever tried before, so a
            # model that could not serve the turn ended it — the four endpoints
            # behind it in the chain were never asked.
            attempts = [req.endpoint, *req.fallbacks]
            last: BaseException | None = None
            result = None
            for i, endpoint in enumerate(attempts):
                try:
                    result = await run_tool_loop(
                        build_chat_adapter(endpoint),
                        messages,
                        system=build_system_prompt(),
                        tools=executor.declarations(),
                        execute=executor,
                        on_event=on_event,
                    )
                    if i:
                        # Say which model actually answered. A silent switch
                        # would leave the transcript attributing an answer to a
                        # model that never produced it.
                        live.publish({
                            "type": "note",
                            "text": (
                                f"{attempts[i - 1].name} could not serve this turn "
                                f"({last}); answered by {endpoint.name} instead."
                            ),
                        })
                    used = endpoint
                    break
                except Exception as exc:
                    last = exc
                    if i + 1 < len(attempts) and worth_another_endpoint(exc):
                        live.publish({
                            "type": "note",
                            "text": f"{endpoint.name} failed ({exc}); trying {attempts[i + 1].name}.",
                        })
                        continue
                    raise
            assert result is not None
            req = replace(req, endpoint=used)

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
        except asyncio.CancelledError:
            # Someone pressed Stop. This is a normal end to a turn, not a
            # failure, and it gets recorded like one: the partial answer is
            # already in the live buffer, and the conversation gets a marker so
            # the transcript explains its own gap instead of just trailing off.
            #
            # Whatever the provider had produced before this point is lost on
            # purpose. Persisting half an assistant message would put a
            # truncated answer into the history every later turn reads as
            # though it were complete.
            try:
                req.conversation.append(
                    "error", "turn stopped by user", {"turn_id": live.turn_id, "cancelled": True}
                )
            except OSError:
                pass
            live.publish({"type": "cancelled", "turn_id": live.turn_id})
            raise
        except Exception as exc:  # noqa: BLE001 — any failure must reach the user
            # The turn is lost either way; what must not be lost is the reason.
            # It is recorded in the conversation so it survives the page, and
            # published so whoever is watching sees it now.
            detail = f"{type(exc).__name__}: {exc}"
            retryable = is_transient(exc)
            try:
                req.conversation.append(
                    "error", detail, {"turn_id": live.turn_id, "retryable": retryable}
                )
            except OSError:
                pass
            live.publish(
                {
                    "type": "error",
                    "turn_id": live.turn_id,
                    "detail": detail,
                    # Whether asking the same thing again has any chance of
                    # working. A provider that is briefly out of capacity says
                    # nothing about the request; a malformed request says
                    # everything about it, and retrying it just fails again.
                    "retryable": retryable,
                }
            )
        finally:
            live.finish()

    async def _ask(self, live: _LiveTurn, request: ApprovalRequest) -> bool:
        """Put an approval to the user and wait.

        The timeout is generous on purpose — a person may have walked away
        mid-review — and expiring is a denial, never a silent yes.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        live.approvals[request.call_id] = fut
        live.publish({"type": "approval_required", **request.to_dict()})
        try:
            return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            live.publish(
                {"type": "approval_resolved", "call_id": request.call_id, "approved": False,
                 "reason": "timed out"}
            )
            return False
        finally:
            live.approvals.pop(request.call_id, None)

    def cancel(self, turn_id: str) -> bool:
        """Stop a running turn. False means there was nothing to stop.

        Cancelling the task is enough: the tool loop is awaiting either the
        provider or an approval, both of which unwind on cancellation, and
        ``_run``'s ``finally`` still closes the turn out and releases every
        reader. A turn parked on an approval is the common case — someone
        started something they did not mean to and does not want to sit through
        the ten-minute timeout to take it back.
        """
        live = self._turns.get(turn_id)
        if live is None or live.done or live.task is None or live.task.done():
            return False
        live.task.cancel()
        return True

    def resolve_approval(self, turn_id: str, call_id: str, approved: bool) -> bool:
        """Answer a pending approval. False means there was nothing to answer."""
        live = self._turns.get(turn_id)
        if live is None:
            return False
        fut = live.approvals.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        live.publish({"type": "approval_resolved", "call_id": call_id, "approved": approved})
        return True

    def pending_approvals(self, turn_id: str) -> list[str]:
        live = self._turns.get(turn_id)
        return [cid for cid, f in live.approvals.items() if not f.done()] if live else []

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
                try:
                    item = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Nothing to say, but the connection has to prove it is
                    # still there. A turn can be silent for minutes — a slow
                    # provider, a long tool call, an approval nobody has
                    # answered — and a stream with no bytes on it is
                    # indistinguishable from a dead one to every intermediary
                    # between here and the browser. They close it, the browser
                    # reports a network error, and the turn goes on running
                    # with nobody watching. This is the byte that stops that.
                    #
                    # Not published: a heartbeat is a property of one reader's
                    # connection, not an event in the turn, and putting it in
                    # the buffer would replay a stale pile of them to whoever
                    # attaches next.
                    yield {"type": "ping"}
                    continue
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
