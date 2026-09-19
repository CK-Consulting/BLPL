"""Making a long conversation fit, when dropping attachments was not enough.

There are two stages to this and they are very different in cost and in what
they lose.

**Stage one** is in chat.py: leave attachments out of the request. It is free,
deterministic, and reversible — the files are still on disk, the transcript
still holds what was read from them, and nothing anybody wrote is touched. On
the conversation that prompted this it took a 1,007,587-token request down to
about 54,000, an eighteen-fold cut, and that is the ordinary case.

**Stage two is this**, and it is the one to reach for last. Summarising costs a
model call and loses detail in a way nobody can predict from the outside — the
user's own observation, and it matches the failure that matters here: tables go
first. A pinout agreed forty turns ago is exactly the kind of thing a summariser
drops, and exactly the kind of thing that must not be lost.

Three things make that tolerable:

* **It is written down.** The summary is appended to the conversation as an
  event of its own, so it is on screen, on disk, and in git like everything
  else. What was lost is at least inspectable.
* **It is computed once.** A later turn reuses the stored summary rather than
  re-summarising, so the conversation does not quietly reword itself every time
  somebody asks a question.
* **The durable record is elsewhere.** Every accepted edit is a commit, and the
  assistant has file_history and read_file_version. So the summariser is told
  to point at the project rather than reproduce it — "the pinmap is in
  overview.md at 66be284" survives compaction in a way a copied table does not.

Turn boundaries are the only place a cut is allowed. A tool call and its result
have to travel together — providers reject a history where one appears without
the other — so the unit is a user message and everything that answered it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blpl.core import llm_chat
from blpl.core.llm_chat import Done, Endpoint, Msg, TextBlock, TextDelta, build_chat_adapter

# The share of a model's window the *text* of a conversation may occupy before
# stage two runs. Well under the whole thing: the tools, the system prompt, any
# attachments that survived stage one, and the answer itself all have to fit
# alongside it, and an answer with no room to be written is not an improvement
# on a request that was too long.
TEXT_SHARE = 0.5

# How much of the recent conversation is never summarised. The last few
# exchanges are what the current question is about, and paraphrasing those is
# how an assistant starts answering a slightly different question than the one
# asked.
KEEP_RECENT_TURNS = 6

SUMMARY_ROLE = "summary"

# Characters per token, by what the characters are. Four is the usual English
# approximation and for prose it holds. Tool results are not prose: they are
# JSON, KiCad S-expressions, netlists, pinout tables and part numbers, none of
# which a tokenizer packs anything like as well — and in a design conversation
# they are the overwhelming majority of the bytes.
#
# Measured on the transcript that prompted this: 67,796 characters of prose
# against 1,078,280 of tool traffic. The flat four-character rule called the
# whole thing 266,000 tokens; the provider billed 476,000. Splitting the two
# rates puts the estimate at 466,000, which is close enough to decide with.
#
# The consequence of the old number was not a slightly-off gauge. It was the
# only guard against sending a request the provider will refuse for length,
# reading 44% under on exactly the conversations that are in danger of it.
_CHARS_PER_TOKEN_PROSE = 4.0
_CHARS_PER_TOKEN_DATA = 2.4

# Bounds on the correction a conversation's own billing history may apply to
# the character estimate. Wide enough to fix a genuinely different mix, narrow
# enough that one odd usage record — a turn that was mostly cache reads, a
# provider that counts differently — cannot swing the decision on its own.
_CALIBRATION_FLOOR = 0.75
_CALIBRATION_CEILING = 2.5

_PROMPT = """\
You are compacting the earlier part of a hardware design conversation so it \
fits in a smaller context window. Write a summary that lets the assistant \
carry on without re-reading what you are replacing.

What must survive, in this order of priority:

1. **Decisions and their reasons.** Which part was chosen, which topology was \
settled, what was ruled out and why. A decision without its reason gets \
re-litigated.
2. **Exact values that were established.** Part numbers, pin assignments, \
voltages, currents, tolerances — verbatim, with their units. Never round, \
never approximate, never write "approximately" where a figure was given.
3. **Open questions**, stated as questions, with what blocks each one.
4. **Corrections.** If something was asserted and later found to be wrong, say \
both, because otherwise the wrong version is what gets remembered.

Where a table was agreed, do not reproduce it from memory. Say what it was and \
where it now lives — the project is a git repository, every accepted edit is a \
commit, and the assistant can read any file at any revision. "The pin budget is \
in dev.05_handheld_core_v1.md as of commit ceedd6d" is worth more than a \
half-remembered copy of it, and cannot be silently wrong.

Do not summarise the summary of a summary: if the text below already contains \
an earlier summary, carry its facts forward intact rather than compressing them \
again.

Write plain prose and lists. No preamble, no sign-off.

CONVERSATION TO COMPACT:
"""


@dataclass(frozen=True)
class Plan:
    """What compaction would do, decided before anything is spent."""

    upto: int          # events [0, upto) get replaced by a summary
    tokens_now: int
    tokens_target: int
    tokens_after: int = 0

    @property
    def worth_it(self) -> bool:
        return self.upto > 0

    @property
    def sufficient(self) -> bool:
        """Whether compacting this far actually gets under budget.

        It does not always. The recent turns are never summarised, and six long
        ones can exceed the budget between them — at which point the honest
        outcome is a request that is still too long, said out loud, rather than
        a paraphrase of the question being asked.
        """
        return self.tokens_after <= self.tokens_target


def estimate(events: list[dict], scale: float = 1.0) -> int:
    """Rough token count for the text of a conversation.

    Two rates rather than one — see ``_CHARS_PER_TOKEN_DATA`` — because a design
    conversation is mostly tool output and tool output does not tokenize like
    English. ``scale`` is the per-conversation correction from ``calibration``,
    applied by ``plan`` so that the trigger and the search for a cut point are
    measured against the same ruler.
    """
    total = 0.0
    for ev in events:
        blocks = (ev.get("metadata") or {}).get("blocks") or []
        # The blocks are what actually travels, so when there are any, they are
        # the whole measurement. `content` is the same prose kept alongside for
        # anything that wants a plain string — both `start_chat_turn` and
        # `persist_messages` write it twice — so adding them together made a
        # conversation look about twice its real size.
        #
        # On the live transcript that was 61 of 111 events counted double, which
        # would start lossy, billable summarisation at roughly half the intended
        # threshold: paraphrasing a conversation that fits comfortably.
        if blocks:
            for b in blocks:
                kind = b.get("type")
                if kind == "text":
                    total += len(b.get("text") or "") / _CHARS_PER_TOKEN_PROSE
                elif kind == "tool_use":
                    total += len(str(b.get("input") or "")) / _CHARS_PER_TOKEN_DATA
                elif kind == "tool_result":
                    total += len(str(b.get("content") or "")) / _CHARS_PER_TOKEN_DATA
        else:
            total += len(ev.get("content") or "") / _CHARS_PER_TOKEN_PROSE
    return int(total * scale)


def calibration(events: list[dict], reserved: int = 0) -> float:
    """What to multiply the character estimate by, learned from this conversation.

    Every assistant turn records what the provider billed for the request that
    produced it, and that request carried exactly the events before it. So the
    conversation has been measuring itself all along: compare the two and the
    ratio is this transcript's own characters-per-token, mix and all.

    Nothing to compare against — a fresh conversation, or a provider that
    reported no usage — leaves the estimate alone rather than guessing.
    """
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("role") != "assistant":
            continue
        usage = (events[i].get("metadata") or {}).get("usage") or {}
        # ``prompt_tokens`` and nothing else. It is the size of the turn's last
        # request — the whole prompt, cached parts included — whereas the
        # ``input_tokens`` beside it is the turn's spend summed over every
        # iteration of the tool loop, which on a ten-iteration turn is about ten
        # times the size of the conversation. Records written before that field
        # existed carry only the sum, so they do not calibrate at all rather
        # than calibrating from the wrong number.
        billed = int(usage.get("prompt_tokens") or 0)
        if billed <= 0:
            continue
        guess = estimate(events[:i]) + reserved
        if guess <= 0:
            continue
        return min(_CALIBRATION_CEILING, max(_CALIBRATION_FLOOR, billed / guess))
    return 1.0


def existing_summary(events: list[dict]) -> tuple[str, int]:
    """The summary already stored, and how many events it covers.

    The *last* one wins: compaction can run more than once on a long-lived
    conversation, and each summary subsumes the one before it.
    """
    text, covers = "", 0
    for ev in events:
        if ev.get("role") == SUMMARY_ROLE:
            covers = int((ev.get("metadata") or {}).get("covers") or 0)
            text = ev.get("content") or ""
    return text, covers


def turn_starts(events: list[dict]) -> list[int]:
    """Indices where a turn begins. The only places a cut is allowed."""
    return [i for i, ev in enumerate(events) if ev.get("role") == "user"]


def plan(events: list[dict], window: int, reserved: int = 0) -> Plan:
    """Whether to compact, and how far.

    ``reserved`` is what the request is already committed to spending on things
    that are not conversation text — attachments that survived stage one, the
    system prompt, the tool declarations.
    """
    budget = max(8_000, int(window * TEXT_SHARE) - reserved)
    scale = calibration(events, reserved)
    now = estimate(events, scale)
    if now <= budget:
        return Plan(upto=0, tokens_now=now, tokens_target=budget, tokens_after=now)

    starts = turn_starts(events)
    _, already = existing_summary(events)
    # The boundary *after* each compactable turn, not the boundary before it.
    #
    # `upto` is exclusive — events[:upto] get summarised — so a cut at the start
    # of the oldest turn summarises nothing. With exactly KEEP_RECENT_TURNS + 1
    # turns that was the only candidate, so a seven-turn conversation that would
    # have fitted after summarising its first turn was sent oversized instead,
    # and refused.
    # `already` is an exclusive end — a stored summary covers events[:already] —
    # so a turn starting *at* it is the first one not yet covered. Using `>`
    # excluded index 0 whenever nothing had been summarised, which quietly made
    # the first turn of every conversation uncompactable.
    eligible = [i for i in starts if i >= already]
    boundaries = eligible[1:]                       # each turn's end
    if len(eligible) > KEEP_RECENT_TURNS:
        candidates = boundaries[: len(eligible) - KEEP_RECENT_TURNS]
    else:
        candidates = []
    if not candidates:
        # Everything left is recent. Compacting further would paraphrase the
        # question being asked, which is worse than a request that is too long
        # and says so.
        return Plan(upto=0, tokens_now=now, tokens_target=budget, tokens_after=now)

    # Walk forward while the tail is still too big, so the cut is as late as it
    # can be — summarise as little as will do.
    cut = candidates[0]
    for i in candidates:
        if estimate(events[i:], scale) <= budget:
            cut = i
            break
        cut = i
    return Plan(
        upto=cut, tokens_now=now, tokens_target=budget, tokens_after=estimate(events[cut:], scale)
    )


def _render(events: list[dict], upto: int) -> str:
    """The stretch being replaced, as plain text for the summariser."""
    lines: list[str] = []
    for ev in events[:upto]:
        role = ev.get("role")
        if role == SUMMARY_ROLE:
            lines.append(f"[earlier summary]\n{ev.get('content', '')}")
            continue
        if role not in ("user", "assistant"):
            continue
        body = (ev.get("content") or "").strip()
        for b in (ev.get("metadata") or {}).get("blocks") or []:
            if b.get("type") == "tool_use":
                # Named, not reproduced. Which files were read is a fact worth
                # keeping; their contents are in the files.
                lines.append(f"[tool] {b.get('name')} {str(b.get('input'))[:200]}")
        if body:
            lines.append(f"{role}: {body}")
    return "\n\n".join(lines)


async def summarise(events: list[dict], upto: int, endpoint: Endpoint) -> str:
    """Ask a model to compact ``events[:upto]``. Raises on failure."""
    adapter = build_chat_adapter(endpoint)
    messages = [Msg(role="user", content=[TextBlock(_PROMPT + _render(events, upto))])]
    out: list[str] = []
    async for event in adapter.stream_chat(messages, max_tokens=8000):
        if isinstance(event, TextDelta):
            out.append(event.text)
        elif isinstance(event, Done):
            break
    text = "".join(out).strip()
    if not text:
        raise RuntimeError("the summariser returned nothing")
    return text


def record(conversation, upto: int, text: str, model: str) -> dict:
    """Store the summary in the conversation itself.

    On disk and on screen like everything else, rather than held in memory for
    the length of a process. A compaction nobody can read is a conversation
    that quietly changed under them.
    """
    return conversation.append(
        SUMMARY_ROLE,
        text,
        {"covers": int(upto), "model": model},
    )


__all__ = [
    "KEEP_RECENT_TURNS",
    "Plan",
    "SUMMARY_ROLE",
    "TEXT_SHARE",
    "estimate",
    "existing_summary",
    "plan",
    "record",
    "summarise",
]
