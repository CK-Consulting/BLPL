"""Stage two: summarising, when dropping attachments was not enough.

The two stages are very different and the order matters. Stage one — leaving
attachments out of the request — is free, deterministic and loses nothing
anybody wrote; on the conversation that prompted all this it cut 1,007,587
tokens to about 54,000. Stage two costs a model call and loses detail
unpredictably, and the detail it loses first is tables. So it runs last, only
when the text alone will not fit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import compaction
from app.chat import history_to_messages
from app.conversations import Conversation


def _user(text: str) -> dict:
    return {"role": "user", "content": text, "metadata": {"blocks": [{"type": "text", "text": text}]}}


def _asst(text: str) -> dict:
    return {"role": "assistant", "content": text, "metadata": {"blocks": [{"type": "text", "text": text}]}}


def _convo(turns: int, size: int = 400) -> list[dict]:
    out: list[dict] = []
    for i in range(turns):
        out.append(_user(f"q{i} " + "x" * size))
        out.append(_asst(f"a{i} " + "y" * size))
    return out


# -- deciding ----------------------------------------------------------------


def test_a_conversation_that_fits_is_left_alone(tmp_path) -> None:
    assert compaction.plan(_convo(3), window=200_000).worth_it is False


def test_a_conversation_that_does_not_fit_is_cut_at_a_turn_boundary() -> None:
    """A tool call and its result have to travel together — providers reject a
    history where one appears without the other — so the unit is a user message
    and everything that answered it."""
    events = _convo(40, size=4000)
    p = compaction.plan(events, window=32_000)
    assert p.worth_it
    assert events[p.upto]["role"] == "user"


def test_the_cut_is_as_late_as_it_can_be() -> None:
    """Summarise as little as will do. Every turn compacted is detail that only
    exists as somebody's paraphrase from then on — so the cut goes as far
    forward as the budget allows, and no further."""
    events = _convo(40, size=1200)
    p = compaction.plan(events, window=32_000)
    assert p.sufficient
    starts = compaction.turn_starts(events)
    before = [i for i in starts if i < p.upto]
    if before:
        # One turn less would not have been enough — otherwise we compacted
        # something we did not need to.
        assert compaction.estimate(events[before[-1] :]) > p.tokens_target


def test_when_the_recent_turns_alone_are_too_big_it_says_so(tmp_path) -> None:
    """It does not always work, and pretending otherwise leaves the provider to
    deliver the bad news without explaining it. Six long exchanges can exceed
    the budget between them, and those are never summarised."""
    # Six exchanges of ~4k tokens each is more than a 32k window leaves for
    # text, and those six are never summarised.
    events = _convo(40, size=9000)
    p = compaction.plan(events, window=32_000)
    assert p.worth_it and not p.sufficient
    assert p.tokens_after > p.tokens_target


def test_the_recent_exchanges_are_never_summarised() -> None:
    """Paraphrasing the last few turns is how an assistant starts answering a
    slightly different question than the one that was asked."""
    events = _convo(40, size=4000)
    p = compaction.plan(events, window=32_000)
    starts = compaction.turn_starts(events)
    assert p.upto <= starts[-compaction.KEEP_RECENT_TURNS]


def test_a_short_conversation_of_huge_turns_refuses_to_compact() -> None:
    """When everything left is recent, a request that is too long and says so
    beats one that has been quietly reworded."""
    events = _convo(3, size=200_000)
    assert compaction.plan(events, window=32_000).worth_it is False


def test_attachments_that_survived_stage_one_are_charged_for() -> None:
    """The text budget is what is left after everything else in the request,
    not the whole window."""
    events = _convo(20, size=4000)
    loose = compaction.plan(events, window=200_000, reserved=0)
    tight = compaction.plan(events, window=200_000, reserved=90_000)
    assert tight.tokens_target < loose.tokens_target


# -- storing and replaying ---------------------------------------------------


def test_a_summary_replaces_what_it_covers_and_nothing_else(tmp_path: Path) -> None:
    conv = Conversation.create(tmp_path, title="t")
    for e in _convo(4, size=50):
        conv.append(e["role"], e["content"], e["metadata"])
    compaction.record(conv, 4, "Earlier: chose the LR2021. VDDop ceiling 3.7 V.", "m")
    conv.append("user", "and now?", {"blocks": [{"type": "text", "text": "and now?"}]})

    msgs = history_to_messages(conv.read_all())
    texts = [m.content[0].text for m in msgs]
    # The summary leads, the covered turns are gone, the rest is verbatim.
    assert "VDDop ceiling 3.7 V" in texts[0]
    assert not any(t.startswith("q0 ") or t.startswith("q1 ") for t in texts)
    assert any(t.startswith("q2 ") for t in texts)
    assert texts[-1] == "and now?"


def test_the_summary_is_computed_once_not_every_turn(tmp_path: Path) -> None:
    """A conversation that quietly rewords itself on every question is worse
    than one that is too long."""
    conv = Conversation.create(tmp_path, title="t")
    for e in _convo(4, size=50):
        conv.append(e["role"], e["content"], e["metadata"])
    compaction.record(conv, 4, "first summary", "m")
    events = conv.read_all()
    text, covers = compaction.existing_summary(events)
    assert (text, covers) == ("first summary", 4)
    # And a second compaction subsumes the first rather than stacking.
    compaction.record(conv, 8, "second summary", "m")
    assert compaction.existing_summary(conv.read_all()) == ("second summary", 8)


def test_a_second_compaction_never_re_covers_what_is_already_summarised() -> None:
    events = _convo(40, size=4000)
    events.insert(0, {"role": "summary", "content": "prior", "metadata": {"covers": 0}})
    # Pretend the stored summary already covers the first twenty entries.
    events[0]["metadata"]["covers"] = 20
    p = compaction.plan(events, window=32_000)
    assert not p.worth_it or p.upto > 20


def test_the_summary_survives_on_disk_and_on_screen(tmp_path: Path) -> None:
    """Written into the conversation like every other event, so what was lost is
    at least inspectable."""
    conv = Conversation.create(tmp_path, title="t")
    conv.append("user", "hi", {"blocks": [{"type": "text", "text": "hi"}]})
    compaction.record(conv, 1, "the summary text", "claude-x")
    stored = [e for e in conv.read_all() if e["role"] == compaction.SUMMARY_ROLE]
    assert stored and stored[0]["content"] == "the summary text"
    assert stored[0]["metadata"] == {"covers": 1, "model": "claude-x"}


# -- summarising -------------------------------------------------------------


def test_the_prompt_asks_for_the_things_that_get_lost() -> None:
    """Values verbatim, decisions with reasons, corrections both ways — and a
    pointer to the file rather than a half-remembered copy of its table, because
    the repository is the durable record and cannot be silently wrong."""
    p = compaction._PROMPT
    for phrase in ("verbatim", "Open questions", "Corrections", "git repository", "commit"):
        assert phrase in p, phrase


def test_a_summariser_that_returns_nothing_is_an_error_not_an_empty_summary() -> None:
    """Recording an empty summary would delete the conversation it replaced."""

    class Silent:
        endpoint = None

        async def stream_chat(self, messages, **kw):
            from blpl.core.llm_chat import Done, Msg

            yield Done(message=Msg(role="assistant", content=[]), stop_reason="end_turn")

    import app.compaction as mod

    original = mod.build_chat_adapter
    mod.build_chat_adapter = lambda ep: Silent()
    try:
        with pytest.raises(RuntimeError, match="returned nothing"):
            asyncio.run(mod.summarise(_convo(2), 2, endpoint=None))  # type: ignore[arg-type]
    finally:
        mod.build_chat_adapter = original


def test_prose_kept_in_two_places_is_counted_once() -> None:
    """Every ordinary event stores the same text in `content` and in a metadata
    text block — start_chat_turn and persist_messages both write it twice — so
    adding them together made a conversation look about twice its real size.

    On the live transcript that was 61 of 111 events counted double, which would
    start lossy, billable summarisation at roughly half the intended threshold:
    paraphrasing a conversation that fits comfortably.
    """
    both = [{"role": "user", "content": "x" * 400,
             "metadata": {"blocks": [{"type": "text", "text": "x" * 400}]}}]
    assert compaction.estimate(both) == 100


def test_an_event_with_no_blocks_still_counts() -> None:
    """Older transcripts, and the error markers, carry only `content`."""
    assert compaction.estimate([{"role": "error", "content": "y" * 400}]) == 100


def test_tool_calls_and_results_are_counted() -> None:
    events = [{"role": "assistant", "content": "", "metadata": {"blocks": [
        {"type": "tool_use", "input": "z" * 200},
        {"type": "tool_result", "content": "w" * 200},
    ]}}]
    assert compaction.estimate(events) > 90


def test_the_oldest_turn_can_be_summarised_on_its_own() -> None:
    """`upto` is exclusive, so a cut at the *start* of the oldest turn
    summarises nothing. With exactly KEEP_RECENT_TURNS + 1 turns that was the
    only candidate, so a seven-turn conversation that would have fitted after
    summarising its first turn was sent oversized and refused instead."""
    events = _convo(compaction.KEEP_RECENT_TURNS + 1, size=8000)
    p = compaction.plan(events, window=32_000)
    assert p.worth_it
    # The cut lands on the second turn's start, so the first turn is what gets
    # replaced — and the six recent exchanges are untouched.
    assert p.upto == compaction.turn_starts(events)[1]
