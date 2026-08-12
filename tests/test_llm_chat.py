"""The chat runtime: the tool loop, and the per-provider wire conversions.

No pytest-asyncio in this repo, so async scenarios run under ``asyncio.run``.
The loop is exercised against a scripted fake adapter rather than a live
provider — what matters here is the loop's own contract (append history, run
tools, feed results back, stop for a stated reason), and a fake makes every
branch of that reachable including the ones a real provider rarely produces.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blpl.core.llm_chat import (
    DEFAULT_MAX_ITERATIONS,
    Done,
    Endpoint,
    Msg,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolDecl,
    ToolResult,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    _to_anthropic_message,
    _to_openai_messages,
    append_usage,
    build_chat_adapter,
    run_tool_loop,
)


class FakeChatAdapter:
    """Replays one scripted turn per call. ``turns`` is a list of event lists."""

    def __init__(self, turns: list[list], name: str = "fake"):
        self.endpoint = Endpoint(name=name, kind="anthropic", model="fake-model")
        self.turns = turns
        self.calls: list[list[Msg]] = []   # history as seen at each turn

    async def stream_chat(self, messages, *, system="", tools=(), max_tokens=0):
        self.calls.append(list(messages))
        events = self.turns[len(self.calls) - 1]
        for event in events:
            yield event


def _assistant(text: str = "", calls: list[ToolUseBlock] | None = None) -> Msg:
    content: list = []
    if text:
        content.append(TextBlock(text))
    content.extend(calls or [])
    return Msg(role="assistant", content=content)


def _turn(text: str = "", calls: list[ToolUseBlock] | None = None, stop: str = "end_turn") -> list:
    events: list = [TextDelta(text)] if text else []
    events.extend(ToolCall(id=c.id, name=c.name, input=c.input) for c in (calls or []))
    events.append(Done(stop_reason=stop, message=_assistant(text, calls)))
    return events


# -- the loop -----------------------------------------------------------------


def test_a_plain_turn_returns_its_text_and_stops() -> None:
    adapter = FakeChatAdapter([_turn("Two 10uF caps should do it.")])
    seen: list = []

    result = asyncio.run(
        run_tool_loop(adapter, [Msg.user("how much bulk cap?")], on_event=seen.append)
    )

    assert result.text == "Two 10uF caps should do it."
    assert result.stop_reason == "end_turn"
    assert result.iterations == 1
    assert len(result.new_messages) == 1
    # The UI sees the delta as it arrives and the done that closes the turn.
    assert [type(e).__name__ for e in seen] == ["TextDelta", "Done"]


def test_a_tool_call_runs_and_its_result_feeds_the_next_turn() -> None:
    call = ToolUseBlock(id="t1", name="read_project_file", input={"path": "design.md"})
    adapter = FakeChatAdapter([
        _turn("Let me look.", [call], stop="tool_use"),
        _turn("It declares 46 components."),
    ])
    ran: list[ToolUseBlock] = []

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        ran.append(c)
        return ToolResultBlock(tool_use_id=c.id, content="# Board\n46 components")

    result = asyncio.run(run_tool_loop(adapter, [Msg.user("what's in it?")], execute=execute))

    assert [c.name for c in ran] == ["read_project_file"]
    assert result.iterations == 2
    assert result.text == "It declares 46 components."
    # assistant → tool results → assistant
    assert [m.role for m in result.new_messages] == ["assistant", "user", "assistant"]
    # The second request saw the whole exchange, results included.
    second = adapter.calls[1]
    assert isinstance(second[-1].content[0], ToolResultBlock)
    assert second[-1].content[0].content == "# Board\n46 components"


def test_every_call_in_one_turn_rides_back_in_one_message(tmp_path) -> None:
    """Providers require a turn's tool results together. Splitting them is the
    classic bug that trains a model out of parallel tool use."""
    calls = [
        ToolUseBlock(id="a", name="read_project_file", input={"path": "a.md"}),
        ToolUseBlock(id="b", name="read_project_file", input={"path": "b.md"}),
    ]
    adapter = FakeChatAdapter([_turn("", calls, stop="tool_use"), _turn("done")])

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=c.id, content=f"body of {c.input['path']}")

    result = asyncio.run(run_tool_loop(adapter, [Msg.user("read both")], execute=execute))

    tool_turn = result.new_messages[1]
    assert tool_turn.role == "user" and len(tool_turn.content) == 2
    assert [b.tool_use_id for b in tool_turn.content] == ["a", "b"]


def test_a_refused_tool_comes_back_as_an_error_result_not_an_exception() -> None:
    """Sandbox denials and un-approved calls are part of the conversation — the
    model must get the chance to try something else."""
    call = ToolUseBlock(id="t1", name="write_file", input={"path": "/etc/passwd"})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use"), _turn("Understood, staying in the project.")])

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=c.id, content="denied: outside the sandbox", is_error=True)

    seen: list = []
    result = asyncio.run(
        run_tool_loop(adapter, [Msg.user("hi")], execute=execute, on_event=seen.append)
    )

    assert result.stop_reason == "end_turn"
    assert result.text == "Understood, staying in the project."
    errored = [e for e in seen if isinstance(e, ToolResult) and e.is_error]
    assert errored and "denied" in errored[0].content


def test_tool_calls_with_no_executor_stop_with_a_named_reason() -> None:
    call = ToolUseBlock(id="t1", name="anything", input={})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use")])
    result = asyncio.run(run_tool_loop(adapter, [Msg.user("hi")]))
    assert result.stop_reason == "tool_use_unsupported"


def test_the_iteration_backstop_reports_itself() -> None:
    call = ToolUseBlock(id="t", name="loop", input={})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use")] * (DEFAULT_MAX_ITERATIONS + 2))

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=c.id, content="again")

    result = asyncio.run(
        run_tool_loop(adapter, [Msg.user("go")], execute=execute, max_iterations=3)
    )
    assert result.stop_reason == "max_iterations" and result.iterations == 3


def test_a_stream_that_never_finishes_is_an_error_not_an_empty_answer() -> None:
    adapter = FakeChatAdapter([[TextDelta("half a thought")]])
    with pytest.raises(RuntimeError, match="without a done event"):
        asyncio.run(run_tool_loop(adapter, [Msg.user("hi")]))


def test_usage_is_collected_across_iterations() -> None:
    call = ToolUseBlock(id="t1", name="x", input={})
    first = [Usage(model="m", input_tokens=10, output_tokens=5), *_turn("", [call], stop="tool_use")]
    second = [Usage(model="m", input_tokens=20, output_tokens=7), *_turn("done")]
    adapter = FakeChatAdapter([first, second])

    async def execute(c):
        return ToolResultBlock(tool_use_id=c.id, content="ok")

    result = asyncio.run(run_tool_loop(adapter, [Msg.user("hi")], execute=execute))
    assert [u.output_tokens for u in result.usage] == [5, 7]


# -- wire conversions ---------------------------------------------------------


def test_anthropic_assistant_turns_replay_provider_bytes_verbatim() -> None:
    """Reasoning blocks carry signatures the API rejects if edited, so a
    re-rendered assistant turn is not good enough — the original must go back."""
    raw = [
        {"type": "thinking", "thinking": "", "signature": "abc123"},
        {"type": "text", "text": "hello"},
    ]
    msg = Msg(role="assistant", content=[TextBlock("hello")], raw_provider="anthropic", raw_content=raw)
    assert _to_anthropic_message(msg)["content"] == raw

    # A message from a different provider has no bytes to preserve, so it is
    # rendered from the normalized blocks.
    other = Msg(role="assistant", content=[TextBlock("hi")], raw_provider="openai", raw_content=[{"x": 1}])
    assert _to_anthropic_message(other)["content"] == [{"type": "text", "text": "hi"}]


def test_anthropic_tool_results_render_as_blocks() -> None:
    msg = Msg.tool_results([ToolResultBlock(tool_use_id="t1", content="body", is_error=False)])
    out = _to_anthropic_message(msg)
    assert out["role"] == "user"
    assert out["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "body", "is_error": False}
    ]


def test_openai_splits_each_tool_result_into_its_own_message() -> None:
    """OpenAI models one result per message; the shared Msg model has to fan out."""
    msg = Msg.tool_results([
        ToolResultBlock(tool_use_id="a", content="one"),
        ToolResultBlock(tool_use_id="b", content="two"),
    ])
    out = _to_openai_messages(msg)
    assert [m["role"] for m in out] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in out] == ["a", "b"]


def test_openai_assistant_tool_calls_serialize_arguments_as_json() -> None:
    msg = _assistant("thinking", [ToolUseBlock(id="c1", name="read", input={"path": "a.md"})])
    (out,) = _to_openai_messages(msg)
    assert out["tool_calls"][0]["function"] == {"name": "read", "arguments": '{"path": "a.md"}'}


def test_plain_user_text_stays_a_plain_string_for_openai() -> None:
    (out,) = _to_openai_messages(Msg.user("hello"))
    assert out == {"role": "user", "content": "hello"}


# -- endpoints and ledger -----------------------------------------------------


def test_endpoint_rejects_unknown_kinds_and_defaults_the_model() -> None:
    with pytest.raises(ValueError, match="unknown endpoint kind"):
        Endpoint.of("gemini")
    assert Endpoint.of("anthropic").model.startswith("claude-")
    # An OpenAI-compatible endpoint borrows OpenAI's default model name.
    assert Endpoint.of("openai-compatible", base_url="http://localhost:8080/v1").model


def test_build_chat_adapter_maps_kinds_to_backends() -> None:
    for kind in ("anthropic", "openai", "openai-compatible", "ollama"):
        adapter = build_chat_adapter(Endpoint.of(kind, "m"))
        assert adapter.endpoint.kind == kind


def test_usage_ledger_appends_jsonl_and_never_breaks_a_turn(tmp_path: Path) -> None:
    ledger = tmp_path / "nested" / "llm_usage.jsonl"
    append_usage(ledger, Usage(model="m", input_tokens=3, output_tokens=4), context={"project": "dev04"})
    append_usage(ledger, Usage(model="m", input_tokens=1, output_tokens=2))
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["output_tokens"] for r in rows] == [4, 2]
    assert rows[0]["project"] == "dev04"

    # An unwritable ledger is bookkeeping lost, not a failed conversation.
    append_usage(tmp_path / "nested" / "llm_usage.jsonl" / "impossible", Usage(model="m"))
