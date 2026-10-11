"""LLM failover: try each provider in order, move on when one fails.

The resolver decides the order (tested in test_llm_resolver); this is the runtime
that walks it. The properties that matter: a single-provider chain behaves exactly
as the bare adapter did (so the CLI path is unchanged, TruncatedResponse and all),
a failing provider falls through to the next, the first success wins, and if every
provider fails the error names all of them.
"""

from __future__ import annotations

import json

import pytest

from blpl.core import llm_adapter as la


class _Fake:
    """A stand-in adapter: either returns a canned dict or raises."""

    def __init__(self, provider: str, result=None, error: Exception | None = None):
        self.provider = provider
        self.model = f"{provider}-model"
        self._result = result
        self._error = error
        self.calls = 0

    def complete_json(self, system, user, output_schema, model=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def test_single_member_delegates_and_reraises_unchanged() -> None:
    """A one-provider chain must not alter behaviour — Stage 1 relies on seeing
    the real TruncatedResponse, not a wrapped one."""
    boom = la.TruncatedResponse("cut off")
    fb = la._FallbackAdapter([_Fake("anthropic", error=boom)])
    with pytest.raises(la.TruncatedResponse):
        fb.complete_json("s", "u", {})


def test_first_provider_success_wins_and_second_is_not_called() -> None:
    a = _Fake("anthropic", result={"rows": [1]})
    b = _Fake("openai", result={"rows": [2]})
    fb = la._FallbackAdapter([a, b])
    assert fb.complete_json("s", "u", {}) == {"rows": [1]}
    assert a.calls == 1 and b.calls == 0


def test_a_failing_provider_falls_through_to_the_next() -> None:
    a = _Fake("anthropic", error=ConnectionError("down"))
    b = _Fake("openai", result={"rows": ["from openai"]})
    fb = la._FallbackAdapter([a, b])
    assert fb.complete_json("s", "u", {}) == {"rows": ["from openai"]}
    assert a.calls == 1 and b.calls == 1


def test_all_providers_failing_raises_an_aggregate_naming_each() -> None:
    a = _Fake("anthropic", error=ConnectionError("network"))
    b = _Fake("openai", error=RuntimeError("bad key"))
    fb = la._FallbackAdapter([a, b])
    with pytest.raises(la.AllProvidersFailed) as exc:
        fb.complete_json("s", "u", {})
    msg = str(exc.value)
    assert "anthropic" in msg and "openai" in msg
    assert len(exc.value.errors) == 2


def test_an_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError):
        la._FallbackAdapter([])


def test_get_adapter_builds_a_fallback_from_the_chain_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "HDM_LLM_CHAIN",
        json.dumps([{"provider": "anthropic", "model": "m1"}, {"provider": "openai", "model": "m2"}]),
    )
    adapter = la.get_adapter()
    assert isinstance(adapter, la._FallbackAdapter)
    assert [m.provider for m in adapter._members] == ["anthropic", "openai"]
    assert [m.model for m in adapter._members] == ["m1", "m2"]


def test_get_adapter_ignores_the_chain_when_a_provider_is_explicit(monkeypatch) -> None:
    """An explicit provider arg is a deliberate single-provider request — it must
    not be silently turned into a fallback chain."""
    monkeypatch.setenv("HDM_LLM_CHAIN", json.dumps([{"provider": "openai", "model": "m"}]))
    adapter = la.get_adapter(provider="anthropic")
    assert adapter.provider == "anthropic"


def test_a_malformed_chain_env_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.setenv("HDM_LLM_CHAIN", "not json{")
    with pytest.raises(ValueError, match="HDM_LLM_CHAIN"):
        la.get_adapter()


def test_the_chain_env_falls_back_to_single_provider_when_empty(monkeypatch) -> None:
    """An empty JSON list means 'no chain' — fall through to the single-provider
    resolution, not a crash."""
    monkeypatch.setenv("HDM_LLM_CHAIN", "[]")
    monkeypatch.setenv("HDM_LLM_PROVIDER", "ollama")
    adapter = la.get_adapter()
    assert adapter.provider == "ollama"


def test_a_task_takes_its_own_chain_when_one_is_declared(monkeypatch) -> None:
    """One process can run several stages, and they can be routed apart.

    ``blpl run --from stage0 --to stage8`` executes stage0 and stage1 in a
    single child. A project routing stage0 to a cheap local model and stage1 to
    an expensive one cannot express that in a single ``HDM_LLM_CHAIN``, so the
    range used to resolve ``default`` and the per-stage route was stored,
    displayed and never consulted.
    """
    monkeypatch.setenv("HDM_LLM_CHAIN", json.dumps([{"provider": "anthropic", "model": "fallback"}]))
    monkeypatch.setenv("HDM_LLM_CHAINS", json.dumps({
        "stage0": [{"provider": "ollama", "model": "cheap"}],
        "stage1": [{"provider": "anthropic", "model": "dear"}],
    }))

    assert [m.model for m in la.get_adapter(task="stage0")._members] == ["cheap"]
    assert [m.model for m in la.get_adapter(task="stage1")._members] == ["dear"]
    # A task with no entry, and a caller that names none, both fall through to
    # the single chain — that is what keeps the agent dispatcher working.
    assert [m.model for m in la.get_adapter(task="vision")._members] == ["fallback"]
    assert [m.model for m in la.get_adapter()._members] == ["fallback"]


def test_an_explicit_provider_still_beats_a_task_chain(monkeypatch) -> None:
    monkeypatch.setenv("HDM_LLM_CHAINS", json.dumps({"stage1": [{"provider": "openai", "model": "m"}]}))
    assert la.get_adapter(provider="anthropic", task="stage1").provider == "anthropic"


def test_a_malformed_task_chain_env_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.setenv("HDM_LLM_CHAINS", "not json{")
    with pytest.raises(ValueError, match="HDM_LLM_CHAINS"):
        la.get_adapter(task="stage1")
