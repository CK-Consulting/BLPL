"""The resolver decides which LLM actually runs, from preference + available keys.

Every branch of "why did it pick that" is pinned here, because that is the
question you ask when a stage uses a model you didn't expect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app.appconfig import AppConfig  # noqa: E402
from app import llm_resolver as r  # noqa: E402


def test_primary_is_the_highest_priority_provider_with_a_key() -> None:
    cfg = AppConfig(llm_priority=["anthropic", "openai"])
    primary = r.resolve_primary(cfg, {"anthropic", "openai"})
    assert primary.provider == "anthropic"
    assert primary.model == cfg.model_for("anthropic")


def test_it_skips_a_preferred_provider_that_has_no_key() -> None:
    """Anthropic is preferred but keyless in the vault, so openai wins — the
    preference is honoured only as far as the keys allow."""
    cfg = AppConfig(llm_priority=["anthropic", "openai"])
    assert r.resolve_primary(cfg, {"openai"}).provider == "openai"


def test_the_chain_is_the_full_fallback_order() -> None:
    cfg = AppConfig(llm_priority=["anthropic", "openai", "ollama"])
    chain = [rp.provider for rp in r.resolve_chain(cfg, {"anthropic", "openai"})]
    assert chain == ["anthropic", "openai", "ollama"]  # ollama is keyless, always usable


def test_the_chain_preserves_declared_order_and_only_drops() -> None:
    cfg = AppConfig(llm_priority=["openai", "anthropic"])
    chain = [rp.provider for rp in r.resolve_chain(cfg, {"anthropic"})]
    assert chain == ["anthropic"], "openai has no key and is dropped; order otherwise kept"


def test_ollama_needs_no_key() -> None:
    cfg = AppConfig(llm_priority=["ollama"])
    assert r.resolve_primary(cfg, set()).provider == "ollama"


def test_no_usable_provider_raises_with_an_actionable_message() -> None:
    cfg = AppConfig(llm_priority=["anthropic", "openai"])
    with pytest.raises(r.NoUsableProvider) as exc:
        r.resolve_primary(cfg, set())
    msg = str(exc.value)
    assert "Settings" in msg and "ollama" in msg  # tells you both ways to fix it


def test_a_key_for_an_unprioritized_provider_is_ignored() -> None:
    """Having a key is necessary, not sufficient — the provider must also be in
    the priority order. A key for openai does nothing if only anthropic is listed."""
    cfg = AppConfig(llm_priority=["anthropic"])
    with pytest.raises(r.NoUsableProvider):
        r.resolve_primary(cfg, {"openai"})
