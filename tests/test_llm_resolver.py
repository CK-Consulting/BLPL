"""The resolver decides which LLM actually runs, from route + available keys.

Every branch of "why did it pick that" is pinned here, because that is the
question you ask when a stage uses a model you didn't expect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app.appconfig import AppConfig, Endpoint  # noqa: E402
from app import llm_resolver as r  # noqa: E402


def _cfg(tasks: dict[str, list[str]] | None = None) -> AppConfig:
    return AppConfig(
        endpoints={
            "claude-main": Endpoint(name="claude-main", kind="anthropic", model="claude-opus-5"),
            "gpt": Endpoint(name="gpt", kind="openai", model="gpt-4o-2024-08-06"),
            "local": Endpoint(name="local", kind="ollama", model="llama3.3"),
        },
        tasks=tasks or {"default": ["claude-main", "gpt"]},
    )


def test_primary_is_the_first_routed_endpoint_with_a_key() -> None:
    primary = r.resolve_primary(_cfg(), {"claude-main", "gpt"})
    assert primary.name == "claude-main" and primary.provider == "anthropic"
    assert primary.model == "claude-opus-5"


def test_it_skips_a_preferred_endpoint_that_has_no_key() -> None:
    """The route is honoured only as far as the keys allow."""
    primary = r.resolve_primary(_cfg(), {"gpt"})
    assert primary.name == "gpt"


def test_the_chain_is_the_full_fallback_order_and_only_drops() -> None:
    cfg = _cfg({"default": ["claude-main", "gpt", "local"]})
    assert [p.name for p in r.resolve_chain(cfg, {"claude-main", "gpt"})] == [
        "claude-main",
        "gpt",
        "local",  # keyless
    ]
    # No key for the first: it is dropped, the rest keep their declared order.
    assert [p.name for p in r.resolve_chain(cfg, {"gpt"})] == ["gpt", "local"]


def test_routes_differ_per_task() -> None:
    """The whole reason the registry exists: a mechanical re-read can run on a
    local model while footprint resolution gets the expensive one."""
    cfg = _cfg({"default": ["claude-main"], "stage0": ["local"], "stage1": ["claude-main", "gpt"]})
    assert r.resolve_primary(cfg, {"claude-main"}, "stage0").name == "local"
    assert r.resolve_primary(cfg, {"claude-main"}, "stage1").name == "claude-main"


def test_a_keyless_endpoint_needs_no_vault_entry() -> None:
    cfg = _cfg({"default": ["local"]})
    primary = r.resolve_primary(cfg, set())
    assert primary.name == "local" and primary.needs_key is False


def test_an_auth_none_endpoint_is_usable_without_a_key() -> None:
    """A local OpenAI-compatible server usually ignores auth entirely."""
    cfg = AppConfig(
        endpoints={
            "vllm": Endpoint(
                name="vllm",
                kind="openai-compatible",
                model="qwen3",
                base_url="http://127.0.0.1:8000/v1",
                auth="none",
            )
        },
        tasks={"default": ["vllm"]},
    )
    primary = r.resolve_primary(cfg, set())
    assert primary.name == "vllm" and primary.base_url == "http://127.0.0.1:8000/v1"


def test_each_endpoint_gets_its_own_key_variable() -> None:
    """Two endpoints of one kind may hold different keys, which a single shared
    ANTHROPIC_API_KEY cannot express."""
    cfg = AppConfig(
        endpoints={
            "work": Endpoint(name="work", kind="anthropic", model="claude-opus-5"),
            "personal": Endpoint(name="personal", kind="anthropic", model="claude-opus-5"),
        },
        tasks={"default": ["work", "personal"]},
    )
    chain = r.resolve_chain(cfg, {"work", "personal"})
    assert [p.key_env for p in chain] == ["BLPL_LLM_KEY__WORK", "BLPL_LLM_KEY__PERSONAL"]


def test_no_usable_endpoint_raises_with_an_actionable_message() -> None:
    with pytest.raises(r.NoUsableProvider) as exc:
        r.resolve_primary(_cfg({"default": ["claude-main"]}), set())
    message = str(exc.value)
    assert "claude-main" in message and "Settings" in message


def test_a_key_for_an_unrouted_endpoint_is_ignored() -> None:
    cfg = _cfg({"default": ["claude-main"]})
    assert [p.name for p in r.resolve_chain(cfg, {"claude-main", "gpt"})] == ["claude-main"]
