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
    EmptyReply,
    DEFAULT_MAX_ITERATIONS,
    _MAX_BREAKPOINTS,
    _mark_cached,
    cache_points,
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
        self.prefixes: list[int] = []      # stable-prefix hint at each turn

    async def stream_chat(self, messages, *, system="", tools=(), max_tokens=0, stable_prefix=0):
        self.calls.append(list(messages))
        self.prefixes.append(stable_prefix)
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


def test_an_empty_reply_is_a_failure_not_a_blank_answer() -> None:
    """A well-formed stream with nothing in it used to end the turn as a
    success: an assistant message with no text and no tool call went into the
    transcript, and the person waiting saw a long pause and then nothing."""
    adapter = FakeChatAdapter([_turn("", stop="end_turn")])
    with pytest.raises(EmptyReply, match="nothing to show") as info:
        asyncio.run(run_tool_loop(adapter, [Msg.user("hi")]))
    assert info.value.stop_reason == "end_turn"
    assert info.value.endpoint_name == "fake"


def test_an_empty_reply_after_tool_calls_is_still_a_failure() -> None:
    """The case seen in practice: a few tool calls, then a blank final turn.
    Nothing reaches ``new_messages`` — persisting the tool exchange without the
    answer would leave a dangling call in the history every retry replays."""
    call = ToolUseBlock(id="t1", name="read_project_file", input={"path": "x"})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use"), _turn("", stop="end_turn")])

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=c.id, content="ok")

    with pytest.raises(EmptyReply):
        asyncio.run(run_tool_loop(adapter, [Msg.user("hi")], execute=execute))


def test_stop_reason_is_stamped_on_every_assistant_message() -> None:
    """The provider's reason for ending each turn travels with the message so
    the transcript can later tell a ``length`` cutoff from a plain ``stop``."""
    call = ToolUseBlock(id="t1", name="x", input={})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use"), _turn("done", stop="max_tokens")])

    async def execute(c: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=c.id, content="ok")

    result = asyncio.run(run_tool_loop(adapter, [Msg.user("hi")], execute=execute))
    assistant = [m for m in result.new_messages if m.role == "assistant"]
    assert [m.stop_reason for m in assistant] == ["tool_use", "max_tokens"]
    assert result.stop_reason == "max_tokens"


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


# ---------------------------------------------------------------------------
# Prompt caching
#
# The failure this guards against is silent and expensive: requests keep
# succeeding, the answers stay correct, and the bill is several times what it
# should be with nothing anywhere saying so. One real hour of it cost $100. So
# these tests assert the shape of the request rather than the shape of the
# reply, because the request is the only place the difference shows.
# ---------------------------------------------------------------------------


def test_the_first_request_of_a_turn_caches_the_settled_history_for_an_hour() -> None:
    # Nothing appended yet, so the whole history is settled and there is one
    # breakpoint. This is the request that pays for the entry the rest read.
    assert cache_points(5, 5) == {4: "1h"}


def test_later_iterations_keep_the_hour_entry_and_roll_a_cheap_one_at_the_tail() -> None:
    points = cache_points(9, 5)
    assert points == {4: "1h", 8: None}
    # The provider requires longer-lived entries to appear before shorter ones.
    assert list(points) == sorted(points)


def test_a_conversation_with_no_settled_history_only_marks_its_tail() -> None:
    assert cache_points(3, 0) == {2: None}
    assert cache_points(0, 0) == {}


def test_breakpoints_never_exceed_what_the_provider_allows() -> None:
    # One is spent on system+tools, so the messages may claim at most three.
    for count, settled in ((1, 1), (2, 1), (50, 20), (9, 5)):
        assert len(cache_points(count, settled)) <= _MAX_BREAKPOINTS - 1


def test_marking_a_message_never_edits_the_provider_bytes_it_was_built_from() -> None:
    # raw_content is written to the conversation file and replayed verbatim on
    # every later request. A cache_control key annotated into it in place would
    # be persisted, replayed, and would change the very prefix it exists to
    # hold still — invalidating the cache on every turn thereafter.
    raw = [{"type": "text", "text": "the rail is 3V3"}]
    msg = Msg(role="assistant", content=[TextBlock("the rail is 3V3")],
              raw_provider="anthropic", raw_content=raw)
    marked = _mark_cached(_to_anthropic_message(msg), "1h")
    assert marked["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert raw == [{"type": "text", "text": "the rail is 3V3"}]


def test_a_rolling_breakpoint_sends_no_ttl_rather_than_a_guessed_literal() -> None:
    marked = _mark_cached({"role": "user", "content": [{"type": "text", "text": "hi"}]}, None)
    assert marked["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_a_breakpoint_lands_on_the_last_block_the_provider_will_take_it_on() -> None:
    # A reasoning block must be replayed untouched, so the marker goes to the
    # last block ahead of it rather than onto it.
    message = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking the pinout"},
            {"type": "thinking", "thinking": "...", "signature": "abc"},
        ],
    }
    marked = _mark_cached(message, "1h")
    assert "cache_control" in marked["content"][0]
    assert "cache_control" not in marked["content"][1]


class _FakeAnthropicStream:
    def __init__(self, final):
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _none():
            return
            yield  # pragma: no cover - an empty async iterator

        return _none()

    async def get_final_message(self):
        return self._final


def _anthropic_payload(messages, *, system="", tools=(), stable_prefix=0, model="claude-opus-5"):
    """Run one Anthropic turn against a stubbed client and return the payload."""
    from types import SimpleNamespace

    sent: list[dict] = []
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        model=model,
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=11, output_tokens=2,
            cache_read_input_tokens=430_000, cache_creation_input_tokens=9_000,
        ),
    )

    class _Messages:
        def stream(self, **payload):
            sent.append(payload)
            return _FakeAnthropicStream(final)

    adapter = build_chat_adapter(Endpoint(name="a", kind="anthropic", model=model))
    adapter._client = lambda: SimpleNamespace(messages=_Messages())

    async def drive():
        out = []
        async for event in adapter.stream_chat(
            messages, system=system, tools=tools, stable_prefix=stable_prefix
        ):
            out.append(event)
        return out

    events = asyncio.run(drive())   # before reading `sent` — drive fills it
    return sent[0], events


def test_anthropic_caches_the_system_prompt_and_the_tools_behind_it_for_an_hour() -> None:
    payload, _ = _anthropic_payload(
        [Msg.user("what rail is U3 on?")],
        system="You are BLPL's hardware design assistant.",
        tools=[ToolDecl("read_file", "read one", {"type": "object"})],
        stable_prefix=1,
    )
    # A block, not a string — a string cannot carry a breakpoint. Tools render
    # ahead of system, so this one marker covers both.
    assert payload["system"] == [{
        "type": "text",
        "text": "You are BLPL's hardware design assistant.",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]


def test_anthropic_marks_the_settled_history_and_the_turn_tail_separately() -> None:
    history = [Msg.user("read core.md"), _assistant("reading"), Msg.user("and the rail?")]
    payload, _ = _anthropic_payload(history, system="s", stable_prefix=2)
    controls = [
        m["content"][-1].get("cache_control") for m in payload["messages"]
    ]
    assert controls == [None, {"type": "ephemeral", "ttl": "1h"}, {"type": "ephemeral"}]


def test_anthropic_reports_what_it_wrote_to_cache_not_just_what_it_read() -> None:
    _, events = _anthropic_payload([Msg.user("hi")], stable_prefix=1)
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.cache_read_tokens == 430_000
    assert usage.cache_creation_tokens == 9_000
    # The number that says how big the conversation actually is. Reading
    # input_tokens alone would call this request 11 tokens.
    assert usage.prompt_tokens == 439_011


def test_the_loop_tells_each_request_how_much_history_was_already_settled() -> None:
    call = ToolUseBlock(id="t1", name="read_file", input={"path": "core.md"})
    adapter = FakeChatAdapter([_turn("", [call], stop="tool_use"), _turn("done")])

    async def execute(c):
        return ToolResultBlock(tool_use_id=c.id, content="# Core")

    asyncio.run(run_tool_loop(adapter, [Msg.user("read core.md")], execute=execute))
    # Two provider turns, and both are told the same seam: the one message the
    # turn began with. Everything after it is this turn's own tool traffic.
    assert adapter.prefixes == [1, 1]
    assert len(adapter.calls[1]) == 3


def test_a_router_serving_an_anthropic_model_gets_breakpoints_and_a_local_one_does_not() -> None:
    router = build_chat_adapter(
        Endpoint(name="or", kind="openai-compatible", model="anthropic/claude-opus-5")
    )
    local = build_chat_adapter(
        Endpoint(name="lm", kind="openai-compatible", model="qwen3.5-4b")
    )
    # cache_control is an Anthropic field. A llama.cpp or vLLM server has no use
    # for it and may reject an unrecognised key outright, so it is never sent.
    assert router._caches() is True
    assert local._caches() is False


def test_the_router_breakpoint_walks_back_off_a_message_that_cannot_carry_one() -> None:
    # Tool results render as role:"tool" messages. Whether a router accepts
    # cache_control on one is not worth finding out through a 400 mid-turn, so
    # the marker moves to the last user message instead; the next request picks
    # the tool results up once its own marker has moved past them.
    adapter = build_chat_adapter(
        Endpoint(name="or", kind="openai-compatible", model="anthropic/claude-opus-5")
    )
    wire = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "read core.md"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "# Core"},
    ]
    adapter._cache(wire, ends=[1, 2, 3], system=True, stable_prefix=1)
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert wire[1]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in wire[3]


def test_the_router_never_downgrades_an_hour_entry_to_a_five_minute_one() -> None:
    adapter = build_chat_adapter(
        Endpoint(name="or", kind="openai-compatible", model="anthropic/claude-opus-5")
    )
    # Both breakpoints walk back to the same user message; the stabler marker
    # placed first must survive, or the hour entry is silently lost.
    wire = [
        {"role": "user", "content": "read core.md"},
        {"role": "tool", "tool_call_id": "t1", "content": "# Core"},
    ]
    adapter._cache(wire, ends=[0, 1], system=False, stable_prefix=1)
    assert wire[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
