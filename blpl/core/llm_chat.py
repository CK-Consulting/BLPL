"""Provider-agnostic chat: message arrays, streaming, and a tool-use loop.

``llm_adapter`` answers one question — "fill in this JSON schema" — and answers it
in one shot. That is the right shape for Stage 0/1 and it is left untouched. It is
the wrong shape for a design conversation, which needs to accumulate history,
stream tokens as they arrive, call tools, read the results, and keep going.

This module is that second shape. It is deliberately *ours* rather than any one
vendor's agent SDK, because the workbench has to mix providers on purpose: a
local ollama endpoint drafting, a frontier model reviewing, several models
reviewing the same evidence independently so their disagreements surface. An SDK
that assumes one vendor cannot express that.

Three concepts:

``Msg``      a normalized message — a role plus content blocks (text, image,
             document, tool_use, tool_result). Providers differ wildly in wire
             format; blocks are the common denominator. Assistant messages also
             carry the provider's own content verbatim (``raw_content``), which
             is what makes multi-turn tool use survive round-tripping: reasoning
             blocks and their signatures must go back *exactly* as they came, and
             no normalized model can promise that. When the same provider is
             continuing its own conversation, the raw form wins.

``ChatEvent`` what happened, as it happens: ``text_delta``, ``tool_call``,
             ``usage``, ``done``. Each serializes to a dict, so the app can
             forward events straight down an SSE stream without a translation
             layer in between.

``run_tool_loop`` the agent loop itself, provider-neutral: stream a turn, hand
             any tool calls to a caller-supplied executor, append the results,
             go again until the model stops asking for tools. Who may call what,
             and whether the user has to approve it, is the executor's business —
             this module never decides policy, it just drives the conversation.

Everything is async. A chat turn is mostly waiting on a socket, and the app
serves these on the same event loop as its other requests.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from .llm_adapter import _DEFAULT_MODELS  # one source of truth for model defaults

# Chat replies are streamed, so the HTTP-timeout ceiling that caps `complete_json`
# does not apply here. The limit that matters is leaving room for a long answer
# plus the tool calls that precede it.
DEFAULT_MAX_TOKENS = 16384

# A backstop, not a budget. A healthy turn reads a few files and proposes an edit
# — well under ten round trips. Hitting this means the model is looping, and the
# loop reports that as its stop reason rather than spinning until the bill says so.
DEFAULT_MAX_ITERATIONS = 32

ProviderKind = Literal["anthropic", "openai", "openai-compatible", "ollama"]


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ImageBlock:
    """An image, base64-encoded. Datasheet pages arrive this way once the
    extraction dispatcher lands; the type exists now so the message model does
    not have to change under a live feature."""

    data: str
    media_type: str = "image/png"
    type: str = "image"


@dataclass(frozen=True)
class DocumentBlock:
    """A PDF, base64-encoded — sent whole where the provider accepts one."""

    data: str
    media_type: str = "application/pdf"
    filename: str | None = None
    type: str = "document"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


Block = TextBlock | ImageBlock | DocumentBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True)
class Msg:
    """One turn. ``raw_content`` is the provider's own rendering of an assistant
    message, kept so it can be replayed verbatim to that same provider.

    This matters more than it looks. Current Anthropic models return reasoning
    blocks that must be passed back unmodified — the API rejects edited ones —
    and a normalized re-render is by definition a modification. Storing the wire
    form alongside the normalized blocks means the UI reads the normalized view
    while the provider gets its own bytes back.
    """

    role: Literal["user", "assistant"]
    content: list[Block]
    raw_provider: str | None = None
    raw_content: Any = None

    @staticmethod
    def user(text: str) -> Msg:
        return Msg(role="user", content=[TextBlock(text)])

    @staticmethod
    def assistant(text: str) -> Msg:
        return Msg(role="assistant", content=[TextBlock(text)])

    @staticmethod
    def tool_results(results: Sequence[ToolResultBlock]) -> Msg:
        """Tool results go back as a user turn — every provider models them that
        way, and all of a turn's results must ride in one message."""
        return Msg(role="user", content=list(results))

    @property
    def text(self) -> str:
        # Separate text blocks are separate paragraphs — providers split them
        # around tool use, and joining with "" glues the end of one sentence to
        # the start of the next.
        return "\n\n".join(
            b.text for b in self.content if isinstance(b, TextBlock) and b.text
        )

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextDelta:
    text: str
    type: str = "text_delta"

    def to_dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict
    type: str = "tool_call"

    def to_dict(self) -> dict:
        return {"type": self.type, "id": self.id, "name": self.name, "input": self.input}


@dataclass(frozen=True)
class ToolResult:
    """Emitted by the loop (not the adapter) once a tool has run, so the UI can
    show what came back without waiting for the model's next words."""

    id: str
    name: str
    content: str
    is_error: bool = False
    type: str = "tool_result"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "content": self.content,
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    type: str = "usage"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


@dataclass(frozen=True)
class Done:
    """End of one provider turn. ``message`` is the authoritative assistant turn
    to append to history — assembled by the backend, which alone knows the exact
    block order and the provider-native form to preserve."""

    stop_reason: str
    message: Msg
    type: str = "done"

    def to_dict(self) -> dict:
        return {"type": self.type, "stop_reason": self.stop_reason, "text": self.message.text}


ChatEvent = TextDelta | ToolCall | ToolResult | Usage | Done


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDecl:
    """What the model is told about a tool. Policy — who may call it, whether it
    needs approval — lives with the executor, not here."""

    name: str
    description: str
    input_schema: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    """A named, resolved place to send a chat turn.

    Carrying the key as a value rather than reading it from the environment is
    deliberate: the app decrypts secrets from the vault into memory for the life
    of a request, two endpoints of the same kind may hold different keys, and
    nothing here should be mutating ``os.environ`` on a shared server.
    """

    name: str
    kind: ProviderKind
    model: str
    api_key: str | None = None
    base_url: str | None = None
    # The most tokens this endpoint will produce, when somebody has stated it.
    # None means "work it out" — see blpl/core/limits.py, which discovers it
    # from the server or learns it from the provider's own refusal.
    max_output_tokens: int | None = None

    @staticmethod
    def of(kind: str, model: str | None = None, **kw: Any) -> Endpoint:
        kind_l = kind.lower()
        if kind_l not in ("anthropic", "openai", "openai-compatible", "ollama"):
            raise ValueError(f"unknown endpoint kind: {kind!r}")
        default_key = "openai" if kind_l == "openai-compatible" else kind_l
        return Endpoint(
            name=kw.pop("name", kind_l),
            kind=kind_l,  # type: ignore[arg-type]
            model=model or _DEFAULT_MODELS.get(default_key, ""),
            **kw,
        )


class ChatAdapter(Protocol):
    """Streams one turn. Implementations must yield a ``Done`` last, always —
    the loop appends its message to history and decides whether to continue."""

    endpoint: Endpoint

    def stream_chat(
        self,
        messages: Sequence[Msg],
        *,
        system: str = "",
        tools: Sequence[ToolDecl] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_schema: dict | None = None,
    ) -> AsyncIterator[ChatEvent]: ...


def build_chat_adapter(endpoint: Endpoint) -> ChatAdapter:
    if endpoint.kind == "anthropic":
        return _AnthropicChat(endpoint)
    if endpoint.kind in ("openai", "openai-compatible"):
        return _OpenAIChat(endpoint)
    if endpoint.kind == "ollama":
        return _OllamaChat(endpoint)
    raise ValueError(f"unknown endpoint kind: {endpoint.kind!r}")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

ToolExecutor = Callable[[ToolUseBlock], Awaitable[ToolResultBlock]]


@dataclass
class LoopResult:
    """What a completed turn produced. ``new_messages`` is everything to append
    to the conversation — assistant turns and the tool-result turns between
    them — so a caller can persist the exchange without reconstructing it."""

    new_messages: list[Msg] = field(default_factory=list)
    stop_reason: str = "end_turn"
    iterations: int = 0
    usage: list[Usage] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The assistant's final prose — what a user reads as 'the answer'."""
        for msg in reversed(self.new_messages):
            if msg.role == "assistant" and msg.text.strip():
                return msg.text
        return ""


async def run_tool_loop(
    adapter: ChatAdapter,
    messages: Sequence[Msg],
    *,
    system: str = "",
    tools: Sequence[ToolDecl] = (),
    execute: ToolExecutor | None = None,
    on_event: Callable[[ChatEvent], None] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopResult:
    """Drive a conversation until the model stops calling tools.

    ``execute`` runs one tool call and returns its result; it owns sandboxing,
    approval, and audit. A call the executor refuses comes back as a result with
    ``is_error`` — refusals are part of the conversation, not exceptions, so the
    model can adapt instead of the turn dying.

    ``on_event`` sees every event including the ones the loop itself emits
    (tool results), which is what the app forwards to the browser.
    """
    history = list(messages)
    result = LoopResult()

    def emit(event: ChatEvent) -> None:
        if on_event is not None:
            on_event(event)

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration
        done: Done | None = None

        async for event in adapter.stream_chat(
            history, system=system, tools=tools, max_tokens=max_tokens
        ):
            if isinstance(event, Done):
                done = event
                continue  # emitted below, after the message is in history
            if isinstance(event, Usage):
                result.usage.append(event)
            emit(event)

        if done is None:
            # A backend that ends its stream without a Done is broken; say so
            # rather than returning a turn that looks empty but successful.
            raise RuntimeError(
                f"{adapter.endpoint.name}: chat stream ended without a done event"
            )

        history.append(done.message)
        result.new_messages.append(done.message)
        emit(done)

        calls = done.message.tool_calls
        if not calls:
            result.stop_reason = done.stop_reason
            return result

        if execute is None:
            # The model asked for a tool nobody can run. Ending here with a
            # distinct reason beats looping or pretending the turn finished.
            result.stop_reason = "tool_use_unsupported"
            return result

        results: list[ToolResultBlock] = []
        for call in calls:
            block = await execute(call)
            results.append(block)
            emit(ToolResult(id=call.id, name=call.name, content=block.content, is_error=block.is_error))

        tool_turn = Msg.tool_results(results)
        history.append(tool_turn)
        result.new_messages.append(tool_turn)

    result.stop_reason = "max_iterations"
    return result


# ---------------------------------------------------------------------------
# Usage ledger
# ---------------------------------------------------------------------------


def append_usage(ledger_path: Path, usage: Usage, *, context: dict | None = None) -> None:
    """Append one usage record as JSONL.

    Same shape and spirit as kicad-happy's dispatcher cost ledger, so the
    datasheet dispatcher and the chat panel report spend the same way. Failing
    to write a ledger line must never take down a working chat turn — the record
    is bookkeeping, the answer is the product.
    """
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **usage.to_dict()}
    if context:
        record.update(context)
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------


class _AnthropicChat:
    """Anthropic streaming chat.

    Two provider facts shape this code. Current models reject sampling
    parameters (``temperature`` and friends return 400), so none are sent.
    And they return reasoning blocks that must be replayed unmodified, which is
    why the assembled message keeps ``raw_content`` and why continuing an
    Anthropic conversation feeds those bytes back rather than a re-render.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    def _client(self):
        import anthropic  # lazy import; optional dep

        kwargs: dict[str, Any] = {
            # The SDK's own default is 2, which is tuned for a short request.
            # A chat turn is not short: it carries the whole design conversation
            # and whatever the assistant was part-way through, so losing one to
            # a few seconds of provider capacity is expensive in a way a
            # one-shot completion is not. Four attempts rides out a brief 529
            # without turning a real outage into a long hang.
            "max_retries": 4,
            # Well past the longest turn observed, and only a ceiling: a turn
            # that finishes sooner is unaffected. The SDK's 10-minute default
            # can expire mid-answer on a genuinely long agentic turn, which
            # reads as a failure rather than as the timeout it is.
            "timeout": 900.0,
        }
        if self.endpoint.api_key:
            kwargs["api_key"] = self.endpoint.api_key
        if self.endpoint.base_url:
            kwargs["base_url"] = self.endpoint.base_url
        return anthropic.AsyncAnthropic(**kwargs)

    async def stream_chat(
        self,
        messages: Sequence[Msg],
        *,
        system: str = "",
        tools: Sequence[ToolDecl] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_schema: dict | None = None,
    ) -> AsyncIterator[ChatEvent]:
        client = self._client()
        payload: dict[str, Any] = {
            "model": self.endpoint.model,
            "max_tokens": max_tokens,
            "messages": [_to_anthropic_message(m) for m in messages],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]

        async with client.messages.stream(**payload) as stream:
            async for event in stream:
                # Text deltas are the only thing worth surfacing live; tool
                # inputs arrive as JSON fragments that are useless until whole,
                # and the final message carries them assembled.
                if getattr(event, "type", None) == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        yield TextDelta(delta.text)
            final = await stream.get_final_message()

        blocks: list[Block] = []
        for block in final.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                blocks.append(TextBlock(block.text))
            elif btype == "tool_use":
                blocks.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input or {})))
                yield ToolCall(id=block.id, name=block.name, input=dict(block.input or {}))

        usage = getattr(final, "usage", None)
        if usage is not None:
            yield Usage(
                model=final.model,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )

        yield Done(
            stop_reason=final.stop_reason or "end_turn",
            message=Msg(
                role="assistant",
                content=blocks,
                raw_provider="anthropic",
                raw_content=[_block_to_dict(b) for b in final.content],
            ),
        )


def _block_to_dict(block: Any) -> Any:
    """Provider SDK object → plain dict, without inspecting what it is.

    Reasoning blocks carry fields (and signatures) this code has no business
    understanding; the whole point is to hand them back untouched.
    """
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    if isinstance(block, dict):
        return block
    return {"type": getattr(block, "type", "text"), "text": getattr(block, "text", str(block))}


def _to_anthropic_message(msg: Msg) -> dict:
    if msg.role == "assistant" and msg.raw_provider == "anthropic" and msg.raw_content:
        return {"role": "assistant", "content": msg.raw_content}
    return {"role": msg.role, "content": [_to_anthropic_block(b) for b in msg.content]}


def _to_anthropic_block(block: Block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
        }
    if isinstance(block, DocumentBlock):
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
        }
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    raise TypeError(f"unsupported block: {block!r}")


# ---------------------------------------------------------------------------
# OpenAI backend (also serves any OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------


class _OpenAIChat:
    """OpenAI chat-completions streaming.

    The same class serves local and third-party servers that speak the OpenAI
    protocol — llama.cpp, vLLM, LM Studio, OpenRouter — because for those the
    only differences are ``base_url`` and whether a key is required. That is the
    cheapest possible path to "bring your own endpoint", which the workbench
    needs for local models.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    def _client(self):
        from openai import AsyncOpenAI  # lazy import; optional dep

        kwargs: dict[str, Any] = {}
        if self.endpoint.api_key:
            kwargs["api_key"] = self.endpoint.api_key
        elif self.endpoint.kind == "openai-compatible":
            # Local servers usually ignore auth, but the SDK refuses to build a
            # client with no key at all. A placeholder keeps the happy path
            # working instead of failing at construction.
            kwargs["api_key"] = "not-required"
        if self.endpoint.base_url:
            kwargs["base_url"] = self.endpoint.base_url
        return AsyncOpenAI(**kwargs)

    async def stream_chat(
        self,
        messages: Sequence[Msg],
        *,
        system: str = "",
        tools: Sequence[ToolDecl] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_schema: dict | None = None,
    ) -> AsyncIterator[ChatEvent]:
        client = self._client()
        wire: list[dict] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            wire.extend(_to_openai_messages(m))

        payload: dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": wire,
            "max_completion_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if json_schema is not None:
            # Constrained decoding, where the server offers it. This is the only
            # fix for malformed JSON that addresses the cause rather than the
            # symptom: vLLM and OpenAI both mask the sampler so a token that
            # would break the schema cannot be chosen, which makes "the model
            # forgot a comma" impossible rather than unlikely. A router that
            # does not support it ignores the field, which is why it is sent
            # unconditionally rather than gated on a capability nobody reports
            # reliably.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "schema": json_schema,
                    "strict": True,
                },
            }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        text_parts: list[str] = []
        # Tool-call arguments stream as JSON fragments spread over many chunks,
        # keyed by index rather than id — accumulate per index, parse at the end.
        partial: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage_event: Usage | None = None

        stream = await client.chat.completions.create(**payload)
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage_event = Usage(
                    model=getattr(chunk, "model", self.endpoint.model),
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                stop_reason = _OPENAI_STOP.get(choice.finish_reason, choice.finish_reason)
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                text_parts.append(delta.content)
                yield TextDelta(delta.content)
            for tc in delta.tool_calls or []:
                slot = partial.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

        blocks: list[Block] = []
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        for _, slot in sorted(partial.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                # Malformed arguments are a real failure mode when a turn is cut
                # off mid-JSON. Pass the raw text through as an error argument so
                # the executor rejects it loudly instead of the loop silently
                # calling a tool with no inputs.
                args = {"__malformed_arguments__": slot["args"]}
            call = ToolUseBlock(id=slot["id"] or f"call_{len(blocks)}", name=slot["name"], input=args)
            blocks.append(call)
            yield ToolCall(id=call.id, name=call.name, input=call.input)

        if usage_event is not None:
            yield usage_event

        yield Done(
            stop_reason=stop_reason,
            message=Msg(role="assistant", content=blocks, raw_provider=self.endpoint.kind),
        )


_OPENAI_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


def _to_openai_messages(msg: Msg) -> list[dict]:
    """One normalized message → one or more OpenAI messages.

    OpenAI splits what other providers keep together: every tool result is its
    own ``role: "tool"`` message, so a turn carrying three results becomes three
    messages. Returning a list rather than a single dict is what lets the same
    ``Msg`` model serve both shapes.
    """
    if msg.role == "assistant":
        text = msg.text
        calls = msg.tool_calls
        out: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.input)},
                }
                for c in calls
            ]
        return [out]

    results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
    if results:
        return [
            {"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content} for r in results
        ]

    parts: list[dict] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
                }
            )
        elif isinstance(block, DocumentBlock):
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": block.filename or "document.pdf",
                        "file_data": f"data:{block.media_type};base64,{block.data}",
                    },
                }
            )
    if len(parts) == 1 and parts[0]["type"] == "text":
        return [{"role": "user", "content": parts[0]["text"]}]
    return [{"role": "user", "content": parts}]


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


class _OllamaChat:
    """Ollama's native chat API.

    Kept separate from the OpenAI-compatible path even though Ollama also serves
    one, because the native API is what `llm_adapter` already targets and it
    reports tool calls whole rather than as JSON fragments. An endpoint of kind
    ``openai-compatible`` pointed at Ollama's ``/v1`` works too — that choice is
    the operator's.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint

    def _client(self):
        import ollama  # lazy import; optional dep

        host = self.endpoint.base_url or os.environ.get("OLLAMA_HOST")
        return ollama.AsyncClient(host=host) if host else ollama.AsyncClient()

    async def stream_chat(
        self,
        messages: Sequence[Msg],
        *,
        system: str = "",
        tools: Sequence[ToolDecl] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
        json_schema: dict | None = None,
    ) -> AsyncIterator[ChatEvent]:
        client = self._client()
        wire: list[dict] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            wire.extend(_to_ollama_messages(m))

        payload: dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": wire,
            "stream": True,
            "options": {"num_predict": max_tokens},
            # Ollama takes the schema directly rather than wrapped in a
            # response_format envelope, and honours it by constraining the
            # sampler the same way.
            **({"format": json_schema} if json_schema is not None else {}),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        text_parts: list[str] = []
        calls: list[ToolUseBlock] = []
        usage_event: Usage | None = None
        stop_reason = "end_turn"

        async for chunk in await client.chat(**payload):
            message = chunk.get("message") or {}
            piece = message.get("content") or ""
            if piece:
                text_parts.append(piece)
                yield TextDelta(piece)
            for i, tc in enumerate(message.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"__malformed_arguments__": args}
                call = ToolUseBlock(id=f"call_{len(calls)}_{i}", name=fn.get("name", ""), input=args)
                calls.append(call)
                yield ToolCall(id=call.id, name=call.name, input=call.input)
            if chunk.get("done"):
                stop_reason = "tool_use" if calls else (chunk.get("done_reason") or "end_turn")
                usage_event = Usage(
                    model=self.endpoint.model,
                    input_tokens=chunk.get("prompt_eval_count") or 0,
                    output_tokens=chunk.get("eval_count") or 0,
                )

        blocks: list[Block] = []
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        blocks.extend(calls)

        if usage_event is not None:
            yield usage_event
        yield Done(
            stop_reason=stop_reason,
            message=Msg(role="assistant", content=blocks, raw_provider="ollama"),
        )


def _to_ollama_messages(msg: Msg) -> list[dict]:
    if msg.role == "assistant":
        out: dict[str, Any] = {"role": "assistant", "content": msg.text}
        if msg.tool_calls:
            out["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.input}} for c in msg.tool_calls
            ]
        return [out]

    results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
    if results:
        return [{"role": "tool", "content": r.content} for r in results]

    images = [b.data for b in msg.content if isinstance(b, ImageBlock)]
    out = {"role": "user", "content": msg.text}
    if images:
        out["images"] = images
    return [out]
