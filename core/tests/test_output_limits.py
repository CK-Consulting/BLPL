"""How many tokens a model will actually produce, per endpoint.

One global ceiling is wrong in both directions. 16,384 — the shared default —
cut every real pinout in half: a correct nRF9151 extraction is 113 pins with
type, power domain, alternate functions and evidence, and measures 21,292
output tokens. Raising it to one big number is no better, because a provider
that caps lower refuses outright and the refusal looks like any other failure.
"""

from __future__ import annotations

import pytest

from blpl.core import limits


class Ep:
    def __init__(self, name="e", model="", kind="anthropic", base_url="", api_key=None,
                 max_output_tokens=None):
        self.name, self.model, self.kind = name, model, kind
        self.base_url, self.api_key = base_url, api_key
        self.max_output_tokens = max_output_tokens


@pytest.fixture(autouse=True)
def _clean():
    limits._LEARNED.clear()
    yield
    limits._LEARNED.clear()


def test_declared_beats_everything() -> None:
    ep = Ep(model="claude-3-5-sonnet", max_output_tokens=64_000)
    assert limits.output_limit(ep) == 64_000


def test_an_unknown_model_gets_a_conservative_opening_bid() -> None:
    """Guessing high costs a round trip; guessing low costs a truncated answer,
    and only one of those is silent."""
    assert limits.output_limit(Ep(model="something-nobody-has-heard-of")) == limits._FALLBACK


def test_the_ceiling_the_caller_wants_is_never_exceeded() -> None:
    """A task that only ever needs 4k should not ask for 200k and make a
    provider reserve it."""
    limits.learn("e", 200_000)
    assert limits.output_limit(Ep(), ceiling=4_000) == 4_000


# -- learning from the provider ----------------------------------------------


def test_vllm_names_its_limit_when_it_refuses() -> None:
    """Verbatim from this network's Nemotron."""
    msg = ("max_tokens=300000 cannot be greater than "
           "max_model_len=max_total_tokens=262144. Please request fewer output tokens.")
    assert limits.limit_in(msg) == 262_144


def test_anthropic_names_its_limit_too() -> None:
    msg = "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens"
    assert limits.limit_in(msg) == 64_000


def test_a_message_with_no_limit_in_it_teaches_nothing() -> None:
    """An overloaded provider must not be read as a ceiling of zero."""
    assert limits.limit_in("Internal server error") is None
    assert limits.learn_from_error("e", RuntimeError("overloaded_error")) is None


def test_a_small_number_in_the_text_is_not_a_limit() -> None:
    """'max_tokens must be at least 1' should not teach a ceiling of 1."""
    assert limits.limit_in("max_tokens must be at least 1") is None


def test_what_a_refusal_taught_is_used_next_time() -> None:
    """The first call to an unfamiliar model should not have to be a sacrifice —
    but the second one certainly should not be."""
    ep = Ep(name="thor", model="nvidia/nemotron-3-super")
    limits.learn_from_error(
        "thor", RuntimeError("max_tokens=300000 cannot be greater than "
                             "max_model_len=max_total_tokens=262144"))
    assert limits.output_limit(ep) == 262_144


def test_a_server_that_advertises_its_window_is_asked(monkeypatch) -> None:
    """A deployment choice, not a property of the weights: the same model served
    differently reports a different number, and the model id cannot tell you."""
    monkeypatch.setattr(limits, "describe", lambda *a: (262_144, None))
    ep = Ep(name="thor", kind="openai-compatible", model="nvidia/nemotron-3-super",
            base_url="http://x/v1")
    # Two thirds: a server's window is prompt *plus* completion, so asking for
    # all of it leaves nowhere to put the question.
    assert limits.output_limit(ep) == int(262_144 * 2 / 3)


def test_a_stated_output_cap_beats_a_window_it_was_derived_from(monkeypatch) -> None:
    """A router that states the output cap is quoting the provider behind it,
    which beats anything derivable from the context window."""
    monkeypatch.setattr(limits, "describe", lambda *a: (1_000_000, 8_192))
    ep = Ep(name="router", kind="openai-compatible", model="google/gemini-3.7-flash",
            base_url="https://openrouter.ai/api/v1")
    assert limits.output_limit(ep) == 8_192


def test_an_unreachable_server_falls_back_rather_than_failing(monkeypatch) -> None:
    monkeypatch.setattr(limits, "describe", lambda *a: (None, None))
    ep = Ep(name="thor", kind="openai-compatible", model="nemotron-x", base_url="http://x/v1")
    assert limits.output_limit(ep) == 32_000  # the nemotron row in the table


def test_only_the_active_model_is_asked_about(monkeypatch) -> None:
    """A self-hosted server lists one thing and a router lists thousands.
    Pulling a catalogue to find one row, on every cold start, per endpoint, is
    not a way to learn a number that does not change."""
    asked: list[str] = []

    def fake_get(url, api_key, timeout=8):
        asked.append(url)
        if url.endswith("/models/google/gemini-3.7-flash"):
            return {"data": {"id": "google/gemini-3.7-flash", "context_length": 1_000_000,
                             "top_provider": {"max_completion_tokens": 65_536}}}
        return None

    monkeypatch.setattr(limits, "_get_json", fake_get)
    ctx, out = limits.describe("https://openrouter.ai/api/v1", "k", "google/gemini-3.7-flash")
    assert (ctx, out) == (1_000_000, 65_536)
    # The single-model route answered, so the catalogue was never fetched.
    assert not any(u.endswith("/models") for u in asked)


def test_the_catalogue_is_the_last_resort(monkeypatch) -> None:
    """Some servers only serve the list. It still works, it is just the fallback."""

    def fake_get(url, api_key, timeout=8):
        if url.endswith("/models"):
            return {"data": [{"id": "other", "context_length": 4}, 
                             {"id": "wanted", "max_model_len": 262_144}]}
        return None

    monkeypatch.setattr(limits, "_get_json", fake_get)
    assert limits.describe("http://x/v1", None, "wanted") == (262_144, None)


def test_a_real_pinout_fits_what_extraction_asks_for() -> None:
    from blpl.agent.tools import datasheets

    assert datasheets.MAX_OUTPUT_TOKENS > 21_292


def test_running_into_the_ceiling_teaches_it_upward(tmp_path, monkeypatch) -> None:
    """A refusal teaches downward; hitting the ceiling teaches upward. Without
    the second half, a model whose real limit is above the table's opening bid
    is truncated forever and never says why — nothing refuses, so nothing is
    learned, and a half-written pinout looks like a model that cannot finish a
    thought."""
    import asyncio

    from blpl.agent.tools import datasheets
    from blpl.core.llm_chat import Done, Msg, TextBlock

    asked: list[int] = []
    full = '[{"numbers": ["1"], "name": "GND"}]'

    class Adapter:
        endpoint = Ep(name="e")

        async def stream_chat(self, messages, *, system="", tools=(), max_tokens=0, stable_prefix=0,
                              json_schema=None):
            asked.append(max_tokens)
            # The schema is handed to the provider, not just written into the
            # prompt: constrained decoding is the only fix for malformed JSON
            # that addresses the cause.
            assert json_schema is not None
            # Cut off on the first, modest ask; complete on the larger one.
            text = full if max_tokens > limits._FALLBACK else full[:12]
            yield Done(message=Msg(role="assistant", content=[TextBlock(text)]),
                       stop_reason="end_turn")

    monkeypatch.setattr(datasheets, "build_chat_adapter", lambda ep: Adapter())
    data, err, _, _ = asyncio.run(
        datasheets._one_task(
            endpoint=Ep(name="e", model="unknown-model"),
            prompt="p", pdf_bytes="", filename="x.pdf",
            schema={"type": "array"}, page_text="text",
        )
    )
    assert not err and data == [{"numbers": ["1"], "name": "GND"}]
    assert len(asked) == 2 and asked[1] > asked[0]
    # And it is remembered, so the next task does not pay for the discovery.
    assert limits.output_limit(Ep(name="e", model="unknown-model")) == asked[1]
