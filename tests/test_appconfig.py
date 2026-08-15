"""blpl.toml is the declarative, non-secret half of the config split.

The properties that matter: the endpoint registry and its task routes round-trip
through save/load unchanged, a missing file yields working defaults, an older
provider-priority config still loads (and keeps the vault entries it already
had), a bogus name is rejected loudly rather than silently shortening a fallback
chain, and no API key ever appears in the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import appconfig  # noqa: E402
from app.appconfig import AppConfig, Endpoint, ProjectEntry  # noqa: E402


def _cfg(**kw) -> AppConfig:
    base = {
        "endpoints": {
            "claude-main": Endpoint(name="claude-main", kind="anthropic", model="claude-opus-5"),
            "local-qwen": Endpoint(
                name="local-qwen",
                kind="openai-compatible",
                model="qwen3-coder",
                base_url="http://127.0.0.1:8080/v1",
                auth="none",
            ),
        },
        "tasks": {"default": ["claude-main"], "stage0": ["local-qwen", "claude-main"]},
    }
    base.update(kw)
    return AppConfig(**base)  # type: ignore[arg-type]


def test_a_missing_file_yields_working_defaults(tmp_path: Path) -> None:
    cfg = appconfig.load(tmp_path / "does-not-exist.toml")
    assert cfg.chain_for("default") == ["anthropic"]
    assert cfg.endpoints["anthropic"].resolved_model()  # a default model, not empty


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    cfg = _cfg(
        projects={"dev04": ProjectEntry(name="dev04", remote="git@github.com:me/dev04.git")}
    )
    appconfig.save(p, cfg)
    back = appconfig.load(p)

    assert sorted(back.endpoints) == ["claude-main", "local-qwen"]
    assert back.endpoints["local-qwen"].base_url == "http://127.0.0.1:8080/v1"
    assert back.endpoints["local-qwen"].auth == "none"
    assert back.chain_for("stage0") == ["local-qwen", "claude-main"]
    assert back.projects["dev04"].remote == "git@github.com:me/dev04.git"


def test_task_order_is_preserved(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    cfg = _cfg(tasks={"default": ["local-qwen", "claude-main"]})
    appconfig.save(p, cfg)
    assert appconfig.load(p).chain_for("default") == ["local-qwen", "claude-main"]


def test_an_unrouted_task_falls_back_to_the_default_chain() -> None:
    """A feature can add a task without anyone editing a config first."""
    cfg = _cfg()
    assert cfg.chain_for("some_future_task") == ["claude-main"]


# -- validation: the whole point is that nothing degrades silently ------------


def test_an_unknown_kind_is_rejected() -> None:
    cfg = _cfg(endpoints={"x": Endpoint(name="x", kind="gemini", model="m")}, tasks={"default": ["x"]})
    with pytest.raises(ValueError, match="unknown kind"):
        cfg.validate()


def test_a_task_routed_to_an_undeclared_endpoint_is_rejected() -> None:
    """The failure this prevents: a typo'd name silently shortens the chain and
    a stage quietly runs on a model nobody chose."""
    cfg = _cfg(tasks={"default": ["claude-main"], "stage1": ["clade-main"]})
    with pytest.raises(ValueError, match="undeclared endpoint"):
        cfg.validate()


def test_an_openai_compatible_endpoint_needs_a_base_url() -> None:
    cfg = _cfg(
        endpoints={"local": Endpoint(name="local", kind="openai-compatible", model="m")},
        tasks={"default": ["local"]},
    )
    with pytest.raises(ValueError, match="base_url"):
        cfg.validate()


def test_a_vision_task_cannot_route_to_an_endpoint_that_cannot_see() -> None:
    """Datasheet extraction hands the model PDF pages. Routing it to a text-only
    endpoint would read nothing and report success."""
    cfg = _cfg(tasks={"default": ["claude-main"], "datasheet_vision": ["local-qwen"]})
    with pytest.raises(ValueError, match="not vision-capable"):
        cfg.validate()

    # …unless the operator says that model really can see.
    cfg.endpoints["local-qwen"].vision = True
    cfg.validate()


def test_an_empty_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        AppConfig().validate()


def test_save_refuses_to_write_an_invalid_config(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    with pytest.raises(ValueError):
        appconfig.save(p, _cfg(tasks={"default": ["nope"]}))
    assert not p.exists()


# -- legacy configs -----------------------------------------------------------


def test_a_legacy_priority_config_still_loads(tmp_path: Path) -> None:
    """Older installs declared three fixed providers. Each becomes an endpoint
    of the same name — which matters beyond nostalgia: the vault entry is keyed
    by that name, so the key they already stored keeps working."""
    p = tmp_path / "blpl.toml"
    p.write_text(
        "[llm]\n"
        'priority = ["openai", "anthropic"]\n\n'
        "[llm.models]\n"
        'anthropic = "claude-opus-5"\n'
        'openai = "gpt-4o-2024-08-06"\n',
        encoding="utf-8",
    )
    cfg = appconfig.load(p)
    assert cfg.chain_for("default") == ["openai", "anthropic"]
    assert cfg.endpoints["anthropic"].kind == "anthropic"
    assert cfg.endpoints["openai"].resolved_model() == "gpt-4o-2024-08-06"


def test_the_rendered_file_is_plain_toml_without_secrets(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    appconfig.save(p, _cfg())
    text = p.read_text()
    assert "[llm.endpoints.claude-main]" in text
    assert "[llm.tasks]" in text
    # No key material and no assignment that could hold any: the file explains
    # where keys live, and never contains one.
    for banned in ("sk-", "api_key =", "apikey", "token ="):
        assert banned not in text.lower()


def test_project_names_with_dots_are_quoted(tmp_path: Path) -> None:
    p = tmp_path / "blpl.toml"
    appconfig.save(p, _cfg(projects={"dev.04": ProjectEntry(name="dev.04")}))
    assert '[projects."dev.04"]' in p.read_text()
    assert "dev.04" in appconfig.load(p).projects
