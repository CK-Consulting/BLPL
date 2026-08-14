"""Where an endpoint's API key comes from: the vault, or the deploy's environment.

docker-compose passes ANTHROPIC_API_KEY / OPENAI_API_KEY into the container from
``.env``, and the compose file has always described them as a fallback. They were
not one: routing counted vaulted keys only, so a container with the key sitting
right there in its environment refused every run with "endpoints with keys:
none". These tests pin both sources, and the order between them.

The environment is global to the process, so every test states the variables it
depends on — including deleting the ones it must not see. The dev machine that
runs this suite has a real ANTHROPIC_API_KEY exported, and without the delenv
the "no key anywhere" cases would pass for the wrong reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

_LLM_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BLPL_LLM_KEY__ANTHROPIC",
             "BLPL_LLM_KEY__WORK", "BLPL_LLM_KEY__PERSONAL")

_CONFIG = """
[llm.endpoints.anthropic]
kind = "anthropic"

[llm.tasks]
default = ["anthropic"]
"""


@pytest.fixture
def main(client, tmp_path, monkeypatch):
    """The reloaded app module, with a one-anthropic-endpoint config and no LLM
    variables in the environment. Exactly the deployed blpl.toml's shape."""
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "data" / "blpl.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(_CONFIG)
    import app.main as m

    return m


def test_the_environment_alone_makes_an_endpoint_usable(main, monkeypatch):
    """The bug: a key in the container's environment and nothing in the vault
    used to resolve to no usable endpoint at all."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-compose")
    cfg = main._load_config()

    assert main.identity.providers_with_keys() == set()  # vault is empty
    assert main._endpoints_with_keys(cfg) == {"anthropic"}
    assert main._endpoint_key(None, cfg, "anthropic") == "sk-from-compose"


def test_no_key_in_either_source_is_still_no_key(main):
    cfg = main._load_config()
    assert main._endpoints_with_keys(cfg) == set()
    assert main._endpoint_key(None, cfg, "anthropic") is None


def test_the_vault_wins_over_the_environment(main, monkeypatch, unlocked):
    """A key typed into Settings is a deliberate statement about this install;
    the environment is only what the deploy happened to be started with."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-compose")
    token = unlocked.cookies.get("blpl_session")
    main.identity.set_secret(token, "anthropic", "sk-from-settings")
    cfg = main._load_config()

    assert main._endpoint_key(token, cfg, "anthropic") == "sk-from-settings"


def test_the_per_endpoint_variable_wins_over_the_provider_wide_one(main, monkeypatch):
    """BLPL_LLM_KEY__ANTHROPIC names one endpoint; ANTHROPIC_API_KEY names a
    kind. The more specific statement is the one that meant something."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-provider-wide")
    monkeypatch.setenv("BLPL_LLM_KEY__ANTHROPIC", "sk-this-endpoint")
    cfg = main._load_config()

    assert main._endpoint_key(None, cfg, "anthropic") == "sk-this-endpoint"


def test_one_global_variable_serves_every_endpoint_of_its_kind(main, monkeypatch, tmp_path):
    """The honest limit of a shared variable. Two Anthropic endpoints and one
    ANTHROPIC_API_KEY means both use it — you say something more precise with
    the vault or the per-endpoint variable, not with this."""
    (tmp_path / "data" / "blpl.toml").write_text(
        """
[llm.endpoints.work]
kind = "anthropic"

[llm.endpoints.personal]
kind = "anthropic"

[llm.tasks]
default = ["work", "personal"]
"""
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shared")
    cfg = main._load_config()

    assert main._endpoints_with_keys(cfg) == {"work", "personal"}
    monkeypatch.setenv("BLPL_LLM_KEY__WORK", "sk-work-only")
    assert main._endpoint_key(None, cfg, "work") == "sk-work-only"
    assert main._endpoint_key(None, cfg, "personal") == "sk-shared"


def test_a_keyless_endpoint_is_unaffected_by_any_of_this(main, tmp_path):
    """ollama takes no key from anywhere; it must not start needing one."""
    (tmp_path / "data" / "blpl.toml").write_text(
        """
[llm.endpoints.local]
kind = "ollama"

[llm.tasks]
default = ["local"]
"""
    )
    cfg = main._load_config()
    # Keyless endpoints are usable without appearing here at all — the resolver
    # admits them on needs_key, not on this set.
    assert main._endpoints_with_keys(cfg) == set()
    assert [p.name for p in main.llm_resolver.resolve_chain(cfg, set())] == ["local"]


def test_a_stage_subprocess_gets_the_environment_key(main, monkeypatch, unlocked):
    """End to end for the reported failure: with only the compose variable set,
    building a stage's environment no longer raises, and the child is handed a
    chain and a key it can actually use."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-compose")
    token = unlocked.cookies.get("blpl_session")

    env = main._inject_llm_env({}, token, "stage1")

    assert env["HDM_LLM_PROVIDER"] == "anthropic"
    assert env["BLPL_LLM_KEY__ANTHROPIC"] == "sk-from-compose"
    assert env["ANTHROPIC_API_KEY"] == "sk-from-compose"
    assert '"endpoint": "anthropic"' in env["HDM_LLM_CHAIN"]


def test_settings_shows_an_env_keyed_endpoint_as_configured(main, monkeypatch, unlocked):
    """Otherwise the screen says the endpoint has no key while runs using it
    succeed — the two facts that made this hard to diagnose."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-compose")

    endpoints = unlocked.get("/api/settings").json()["endpoints"]
    anthropic = next(e for e in endpoints if e["name"] == "anthropic")

    assert anthropic["has_key"] is True
    assert anthropic["key_source"] == "env"


def test_settings_never_reports_a_key_it_does_not_have(main, unlocked):
    endpoints = unlocked.get("/api/settings").json()["endpoints"]
    anthropic = next(e for e in endpoints if e["name"] == "anthropic")

    assert anthropic["has_key"] is False
    assert anthropic["key_source"] == ""
