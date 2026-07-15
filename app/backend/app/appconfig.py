"""Declarative, non-secret app configuration in ``blpl.toml``.

The user asked for a nix-like split: everything that isn't a secret lives in a
plaintext file you can read, diff, and commit; only the secrets go in the
encrypted DB. This is that file.

It holds:
  - the LLM provider *priority order* and per-provider model choice — which
    model runs first, and what falls back to what when a key is missing or a
    call fails. The keys themselves are never here; only which providers you
    intend to use and in what order.
  - the *project registry*: name → git remote, so the server knows what to clone
    and pull. No credentials — auth to the remote is the deploy's business
    (a deploy key or a token in the environment), not this file's.

TOML because it is the format the pipeline's project.yaml neighbours already
feel like, and because ``tomllib`` reads it from the stdlib. Writing needs a
third-party lib, so we render it by hand — the shape is small and fixed, and a
hand-rendered file stays diff-friendly and comment-free rather than reordered by
a serializer.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The providers the LLM adapter knows how to drive. A priority list may only name
# these; anything else is a typo we should reject rather than silently skip.
KNOWN_PROVIDERS = ("anthropic", "openai", "ollama")

_DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o-2024-08-06",
    "ollama": "llama3.3",
}


@dataclass
class ProjectEntry:
    name: str
    remote: str = ""  # git remote URL; empty for a local-only project
    branch: str = "main"


@dataclass
class AppConfig:
    # Order matters: index 0 is tried first, and each subsequent entry is the
    # fallback for the one before it.
    llm_priority: list[str] = field(default_factory=lambda: ["anthropic"])
    llm_models: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_MODELS))
    projects: dict[str, ProjectEntry] = field(default_factory=dict)

    def model_for(self, provider: str) -> str:
        return self.llm_models.get(provider, _DEFAULT_MODELS.get(provider, ""))

    def validate(self) -> None:
        """Reject a config that names a provider the adapter can't drive.

        A misspelled provider in the priority list would otherwise degrade
        silently to a shorter fallback chain — exactly the kind of quiet failure
        this codebase keeps getting burned by."""
        unknown = [p for p in self.llm_priority if p not in KNOWN_PROVIDERS]
        if unknown:
            raise ValueError(
                f"unknown LLM provider(s) in priority list: {unknown}. "
                f"Known providers: {list(KNOWN_PROVIDERS)}"
            )
        if not self.llm_priority:
            raise ValueError("llm_priority must name at least one provider")


def load(path: Path) -> AppConfig:
    """Read blpl.toml, or return defaults if it does not exist yet.

    A missing file is not an error: a fresh install has no config, and the
    defaults (Anthropic first, stock models, no projects) are a working starting
    point the settings UI then edits.
    """
    path = Path(path)
    if not path.exists():
        return AppConfig()

    with path.open("rb") as f:
        data = tomllib.load(f)

    llm = data.get("llm", {})
    priority = llm.get("priority") or ["anthropic"]
    models = dict(_DEFAULT_MODELS)
    models.update(llm.get("models", {}))

    projects: dict[str, ProjectEntry] = {}
    for name, entry in (data.get("projects", {}) or {}).items():
        projects[name] = ProjectEntry(
            name=name,
            remote=str(entry.get("remote", "")),
            branch=str(entry.get("branch", "main")),
        )

    cfg = AppConfig(llm_priority=list(priority), llm_models=models, projects=projects)
    cfg.validate()
    return cfg


def save(path: Path, cfg: AppConfig) -> None:
    """Render blpl.toml. Validates first — we never write a config that would
    fail to load."""
    cfg.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(cfg), encoding="utf-8")


def _render(cfg: AppConfig) -> str:
    lines = [
        "# BLPL app configuration. Non-secret and declarative — commit it.",
        "# API keys are NOT here; they live encrypted in the vault DB.",
        "",
        "[llm]",
        "# Providers are tried in this order; each is the fallback for the one before.",
        f"priority = {_toml_str_list(cfg.llm_priority)}",
        "",
        "[llm.models]",
    ]
    for provider in KNOWN_PROVIDERS:
        if provider in cfg.llm_models:
            lines.append(f'{provider} = "{cfg.llm_models[provider]}"')
    lines.append("")

    for name in sorted(cfg.projects):
        entry = cfg.projects[name]
        lines.append(f"[projects.{_toml_key(name)}]")
        lines.append(f'remote = "{entry.remote}"')
        lines.append(f'branch = "{entry.branch}"')
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _toml_str_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _toml_key(name: str) -> str:
    # Bare keys are safe only for [A-Za-z0-9_-]; anything else gets quoted.
    if name.replace("-", "").replace("_", "").isalnum():
        return name
    return f'"{name}"'
