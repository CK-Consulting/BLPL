"""blpl.toml is the declarative, non-secret half of the config split.

The properties that matter: it round-trips through save/load unchanged, a missing
file yields working defaults rather than an error, a bogus provider name is
rejected loudly (not silently dropped), and no API key ever appears in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import appconfig  # noqa: E402
from app.appconfig import AppConfig, ProjectEntry  # noqa: E402


def test_a_missing_file_yields_working_defaults(tmp_path: Path) -> None:
    cfg = appconfig.load(tmp_path / "does-not-exist.toml")
    assert cfg.llm_priority == ["anthropic"]
    assert cfg.model_for("anthropic")  # a default model, not empty


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    cfg = AppConfig(
        llm_priority=["openai", "anthropic", "ollama"],
        llm_models={"anthropic": "claude-opus-4-8", "openai": "gpt-4o-2024-08-06", "ollama": "llama3.3"},
        projects={"dev04": ProjectEntry(name="dev04", remote="git@github.com:me/dev04.git", branch="main")},
    )
    appconfig.save(p, cfg)
    back = appconfig.load(p)
    assert back.llm_priority == ["openai", "anthropic", "ollama"]
    assert back.model_for("openai") == "gpt-4o-2024-08-06"
    assert back.projects["dev04"].remote == "git@github.com:me/dev04.git"


def test_priority_order_is_preserved(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    appconfig.save(p, AppConfig(llm_priority=["ollama", "openai", "anthropic"]))
    assert appconfig.load(p).llm_priority == ["ollama", "openai", "anthropic"]


def test_an_unknown_provider_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown LLM provider"):
        AppConfig(llm_priority=["anthropic", "claude-3"]).validate()


def test_an_empty_priority_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        AppConfig(llm_priority=[]).validate()


def test_save_refuses_to_write_an_invalid_config(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    with pytest.raises(ValueError):
        appconfig.save(p, AppConfig(llm_priority=["nonsense"]))
    assert not p.exists(), "an invalid config must not be written even partially"


def test_the_rendered_file_is_plain_toml_without_secrets(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    appconfig.save(p, AppConfig(llm_priority=["anthropic"]))
    text = p.read_text()
    # It is a comment-carrying, human-diffable file, and it says so.
    assert "[llm]" in text
    assert "priority" in text
    assert "NOT here" in text  # the "keys are not in this file" reminder
    # tomllib must accept what we wrote.
    import tomllib
    tomllib.loads(text)


def test_project_names_with_dots_are_quoted(tmp_path: Path) -> None:
    """dev.04 is a real project name; a bare TOML key cannot contain a dot."""
    p = tmp_path / "blpl.toml"
    appconfig.save(p, AppConfig(projects={"dev.04": ProjectEntry(name="dev.04", remote="")}))
    reloaded = appconfig.load(p)
    assert "dev.04" in reloaded.projects
