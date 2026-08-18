"""Running a tool call: vet, approve, execute, record.

The order matters and is not arbitrary.

1. **Look up the spec.** An unknown name is a refusal, not a crash.
2. **Vet paths through the sandbox** — before approval. A call that could never
   be permitted must not become a dialog someone clicks through; approval
   fatigue is a real attack surface, and every question that did not need
   asking makes the next one cheaper to wave past.
3. **Ask, if policy says so.** ``ask`` remembers the answer for the conversation
   (the caller owns the set, since an executor lives only for one turn);
   ``ask_always`` does not, because a mutation you approved an hour ago is not
   consent for the next one.
4. **Execute**, catching everything: a tool that throws returns an error result
   the model can read and adapt to, rather than killing the turn.
5. **Record** — every call, approved or refused, lands in the audit trail.

A refusal is a normal outcome here, not an exception path. That is what lets an
agent hit a wall and route around it instead of the conversation dying.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from blpl.core.llm_chat import ToolResultBlock, ToolUseBlock

from ..references import ReferencePolicyError
from .toolspec import ApprovalRequest, ToolContext, ToolDenied, ToolSpec

# Truncate huge tool results before they reach the model. A whole artifact can
# be legitimate; a megabyte of it is never what the caller meant.
_MAX_RESULT_CHARS = 60_000

ApprovalFn = Callable[[ApprovalRequest], Awaitable[bool]]
RecordFn = Callable[[dict], None]


def _summarize(spec: ToolSpec, args: dict) -> str:
    """A one-line description of the act, for the approval prompt.

    Written from the arguments rather than the model's own words: the model
    should not get to phrase the question the user is answering.
    """
    interesting = [str(args[k]) for k in ("path", "name", "mpn", "query", "file") if args.get(k)]
    if not interesting:
        interesting = [json.dumps(args)[:120]]
    return f"{spec.name}: {', '.join(interesting)[:200]}"


class ToolExecutor:
    """Binds a tool set to one project and turn."""

    def __init__(
        self,
        specs: list[ToolSpec],
        ctx: ToolContext,
        *,
        approve: ApprovalFn | None = None,
        record: RecordFn | None = None,
        session_approved: set[str] | None = None,
    ):
        self.specs = {s.name: s for s in specs}
        self.ctx = ctx
        self._approve = approve
        self._record = record
        # Owned by the caller when supplied, because an executor lives for one
        # turn and "the session" means the conversation. Keeping this set here
        # made `ask` behave exactly like `ask_always`: the memory was thrown
        # away with the executor, so every message re-asked to look up the same
        # part. An approval prompt that appears on every message is one people
        # learn to dismiss without reading, which is worse than not asking.
        self._session_approved: set[str] = (
            session_approved if session_approved is not None else set()
        )
        self._seq = 0

    def declarations(self):
        return [s.to_decl() for s in self.specs.values()]

    async def __call__(self, call: ToolUseBlock) -> ToolResultBlock:
        self._seq += 1
        args = dict(call.input or {})

        def done(content: str, *, is_error: bool = False, status: str = "ok") -> ToolResultBlock:
            if self._record:
                self._record(
                    {
                        "seq": self._seq,
                        "tool": call.name,
                        "args": args,
                        "status": status,
                        # A digest rather than the body: the audit trail should
                        # say what happened without becoming a second copy of
                        # every file the agent ever read.
                        "result_digest": content[:200],
                    }
                )
            return ToolResultBlock(tool_use_id=call.id, content=content, is_error=is_error)

        spec = self.specs.get(call.name)
        if spec is None:
            return done(f"unknown tool {call.name!r}", is_error=True, status="unknown_tool")
        if "__malformed_arguments__" in args:
            return done(
                "tool arguments were not valid JSON; call the tool again with complete arguments",
                is_error=True,
                status="malformed_args",
            )

        # -- 2. sandbox, before any question is asked -------------------------
        try:
            self._vet_paths(spec, args)
        except ToolDenied as exc:
            return done(f"refused: {exc}", is_error=True, status="denied_sandbox")

        # -- 3. approval ------------------------------------------------------
        needs_ask = spec.approval == "ask_always" or (
            spec.approval == "ask" and spec.name not in self._session_approved
        )
        if needs_ask:
            if self._approve is None:
                return done(
                    f"{spec.name} needs approval, and this run has no way to ask for it",
                    is_error=True,
                    status="denied_no_approver",
                )
            request = ApprovalRequest(
                call_id=call.id,
                tool=spec.name,
                kind=spec.kind,
                args=args,
                summary=_summarize(spec, args),
            )
            try:
                approved = await self._approve(request)
            except asyncio.TimeoutError:
                return done(
                    f"{spec.name} was not approved in time — nothing was done",
                    is_error=True,
                    status="approval_timeout",
                )
            if not approved:
                return done(
                    f"the user declined {spec.name}. Do not retry it; ask what they would prefer.",
                    is_error=True,
                    status="denied_user",
                )
            if spec.approval == "ask":
                self._session_approved.add(spec.name)

        # -- 4. execute -------------------------------------------------------
        try:
            content = await spec.handler(self.ctx, args)
        except ToolDenied as exc:
            return done(f"refused: {exc}", is_error=True, status="denied")
        except ReferencePolicyError as exc:
            return done(f"refused by the project sandbox: {exc}", is_error=True, status="denied_sandbox")
        except FileNotFoundError as exc:
            return done(f"not found: {exc}", is_error=True, status="not_found")
        except Exception as exc:  # noqa: BLE001 — a tool failure is a result, not a crash
            return done(f"{type(exc).__name__}: {exc}", is_error=True, status="error")

        if len(content) > _MAX_RESULT_CHARS:
            head = int(_MAX_RESULT_CHARS * 0.6)
            content = (
                content[:head]
                + f"\n\n… {len(content) - _MAX_RESULT_CHARS} characters elided from the middle …\n\n"
                + content[-(_MAX_RESULT_CHARS - head) :]
            )
        return done(content)

    # -- path vetting --------------------------------------------------------

    def _vet_paths(self, spec: ToolSpec, args: dict) -> None:
        for arg in spec.path_args:
            raw = args.get(arg)
            if not raw:
                continue
            target = self._resolve(str(raw))
            try:
                if arg in spec.write_args:
                    self.ctx.sandbox.check_write(target)
                else:
                    self.ctx.sandbox.check_read(target)
            except ReferencePolicyError as exc:
                raise ToolDenied(str(exc)) from exc

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (self.ctx.project_dir / p)
