"""Unit tests for pipeline.llm_adapter.

Only the factory + error paths are tested here. Provider backends require network
access and API keys, so they're exercised via Phase 2 integration tests (separate,
opt-in), not in unit tests.
"""

from __future__ import annotations

import pytest

from blpl.core import llm_adapter as la


def test_get_adapter_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDM_LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="unknown HDM_LLM_PROVIDER"):
        la.get_adapter(provider="bogus")


def test_get_adapter_honours_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HDM_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HDM_LLM_MODEL", "gpt-test")
    a = la.get_adapter()
    assert a.provider == "openai"
    assert a.model == "gpt-test"


def test_get_adapter_default_is_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HDM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("HDM_LLM_MODEL", raising=False)
    a = la.get_adapter()
    assert a.provider == "anthropic"
